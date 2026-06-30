"""Interactive curses TUI for libwatchduty (redesigned).

POSIX-only. Stdlib-only at the view layer: :mod:`curses`, :mod:`threading`,
:mod:`queue`, :mod:`time`, :mod:`locale`, :mod:`html`, :mod:`re`,
:mod:`signal`, :mod:`textwrap`. Network I/O goes through
:class:`libwatchduty.client.WatchDutyClient` (which uses ``requests``); all
calls stay on a worker thread.

Layout (matches design_handoff_wildfire_tui/README.md):

    ┌──────────────────────────────────────────────────────────┐
    │ status bar (◉ watchduty · ⌖ near · sort ▼ THREAT · LIVE) │
    ├──────────────────────┬───────────────────────────────────┤
    │ FIRE LIST (~42%)     │ DETAIL PANE (~58%)                │
    │ THREAT|DIR|DIST|...  │  title · KV (threat,wind,spark…)  │
    │ ▰ bar + bearing ↗    │  tabs: Updates / Radio / Map/Evac │
    │ legend row           │  panel (wrapped, scrollbar right) │
    ├──────────────────────┴───────────────────────────────────┤
    │ keybind footer                                            │
    └──────────────────────────────────────────────────────────┘

Entry point: :func:`run` — launches the curses app loop.
"""

from __future__ import annotations

import curses
import html
import locale
import os
import queue
import errno
import fcntl
import pty
import re
import shutil
import signal
import struct
import subprocess
import termios
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from math import (
    asin, atan2, cos, degrees, inf, log, log10, pi, radians, sin, sqrt,
)
from typing import Any

from .client import GEO_EVENT_TYPES, WatchDutyClient, WatchDutyError
from . import aircraft as _aircraft

# Be polite to api.watchduty.org.
_MIN_AUTO_REFRESH = 30
_LIVE_POLL_SECONDS = 30
# Bulk-sweep visible set only when the list is small. Above this threshold
# we lazy-load just the selected fire + a neighborhood window.
_BULK_PREFETCH_THRESHOLD = 24
# Neighborhood prefetch radius (reports only) around the selected fire —
# keeps j/k navigation instant on large lists without hammering the API.
_NEIGHBOR_REPORT_WINDOW = 4
# Hard cap on cached report lists; oldest dropped FIFO when exceeded.
_REPORTS_CACHE_MAX = 200
_MIN_LINES = 10
_MIN_COLS = 40
_REPORTS_RENDER_LIMIT = 40
# Spec: updates feed content capped at `max-width: 760px` (~96 cells).
_DETAIL_MAX_CONTENT = 96
# Spec: list ≈ 42% but capped so wide terminals don't leave dead gutter.
_LIST_W_MAX = 64
# Per-card layout: 1 fire = 2 rows (zebra stripe gives separation, no spacer).
_LIST_ROWS_PER_FIRE = 2

# Image-size presets for inline thumbnails (slot_h, slot_w_max).
_IMG_SIZE_PRESETS: dict[str, tuple[int, int]] = {
    "small": (6,  32),
    "med":   (10, 60),
    "large": (16, 84),
}
_IMG_SIZE_ORDER = ("small", "med", "large")
_ERROR_TTL = 5.0
_CHORD_TIMEOUT = 0.5
_TICK_MS = 200
_HISTORY_LIMIT = 24  # sparkline data points per fire

# Threat scoring knobs (per README §Threat Scoring).
_SIZE_REF = 1000.0
_GROWTH_GAIN = 0.5
_WIND_GAIN = 0.04

# Sort cycle. "threat" is the default when --near is set.
_SORT_KEYS = ("threat", "distance", "acreage", "updated")

# Detail-pane tabs + hotkey jumpers (1-4 + the spec's "keep old hotkeys").
_TABS = ("updates", "radio", "map", "evac")
_TAB_KEYS: dict[int, str] = {
    ord("1"): "updates", ord("2"): "radio",
    ord("3"): "map",     ord("4"): "evac",
    ord("u"): "updates", ord("R"): "radio",
    ord("c"): "map",     ord("e"): "evac",
}

# Worker-request kinds.
_REQ_REFRESH_FIRES = "REFRESH_FIRES"
_REQ_LOAD_REPORTS = "LOAD_REPORTS"
_REQ_LOAD_RADIO = "LOAD_RADIO"
_REQ_LOAD_CAMS = "LOAD_CAMS"
_REQ_LOAD_FPS = "LOAD_FPS"
_REQ_LOAD_AIRCRAFT = "LOAD_AIRCRAFT"
_REQ_FETCH_IMAGE = "FETCH_IMAGE"

# Focus regions.
_FOCUS_LIST = "list"
_FOCUS_DETAIL = "detail"

# Glyphs.
_THREAT_FULL = "▰"
_THREAT_EMPTY = "▱"
_SPARK_RAMP = "▁▂▃▄▅▆▇█"
_ARROWS = ("↑", "↗", "→", "↘", "↓", "↙", "←", "↖")
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _strip_html(s: str) -> str:
    """Strip HTML tags + entities to a single-line string."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</p>\s*<p>", " | ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return " ".join(s.split())


def _split_html_lines(s: str) -> list[str]:
    """Like ``_strip_html`` but keeps paragraph / list / br breaks as
    separate non-empty lines so callers can render each one on its own row.
    """
    if not s:
        return []
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(?:p|li|ul|ol|div|h[1-6])[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    out: list[str] = []
    for part in s.split("\n"):
        cleaned = " ".join(part.split())
        if cleaned:
            out.append(cleaned)
    return out


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km."""
    lat1, lng1 = a
    lat2, lng2 = b
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(h))


def _initial_bearing(
    a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Initial great-circle bearing from a → b, degrees in [0, 360)."""
    la1 = radians(a[0])
    la2 = radians(b[0])
    dlng = radians(b[1] - a[1])
    y = sin(dlng) * cos(la2)
    x = cos(la1) * sin(la2) - sin(la1) * cos(la2) * cos(dlng)
    return (degrees(atan2(y, x)) + 360.0) % 360.0


def _bearing_arrow(deg: float | None) -> str:
    """Pick an 8-point compass arrow glyph."""
    if deg is None:
        return "·"
    idx = int(((deg % 360.0) / 45.0) + 0.5) % 8
    return _ARROWS[idx]


def _bearing_compass(deg: float | None) -> str:
    """8-point compass label (`N`, `NE`, …)."""
    if deg is None:
        return ""
    idx = int(((deg % 360.0) / 45.0) + 0.5) % 8
    return _COMPASS[idx]


def _wrap_around_image(
    text: str, narrow_w: int, wide_w: int, narrow_rows: int,
) -> list[str]:
    """Greedy word-wrap: first ``narrow_rows`` lines use ``narrow_w``
    (so they fit beside an inline image), the rest use ``wide_w``.
    Returns at least one (possibly empty) line."""
    if not text or not text.strip():
        return [""]
    words = text.split()
    lines: list[str] = []
    cur = ""

    def cap() -> int:
        return narrow_w if len(lines) < narrow_rows else wide_w

    for w in words:
        if not cur:
            cur = w
            continue
        if len(cur) + 1 + len(w) <= cap():
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _sparkline(values: list[float], width: int = 4) -> str:
    """Map ``values`` to the `▁▂▃▄▅▆▇█` ramp, last ``width`` samples."""
    if not values or width <= 0:
        return ""
    vs = list(values)[-width:]
    lo, hi = min(vs), max(vs)
    if hi == lo:
        return _SPARK_RAMP[3] * len(vs)
    rng = hi - lo
    return "".join(
        _SPARK_RAMP[min(7, int(round((v - lo) / rng * 7)))] for v in vs
    )


def _safe_str(s: Any) -> str:
    """Coerce to a printable string the locale can render."""
    if s is None:
        return ""
    text = str(s)
    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        text.encode(enc)
        return text
    except UnicodeEncodeError:
        return text.encode("ascii", "replace").decode("ascii")


def _seconds_since_iso(iso: str) -> float:
    """Seconds since an ISO-8601 timestamp; 0.0 on parse error."""
    if not iso:
        return 0.0
    try:
        from datetime import datetime, timezone
        s = iso.rstrip("Z")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return 0.0


def _format_age(seconds: float) -> str:
    """Compact relative duration (`12s`, `3m`, `2h`, `4d`)."""
    if seconds < 0:
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _is_planned(fire: dict) -> bool:
    """Heuristic: prescribed/planned burn → keep threat near zero."""
    data = fire.get("data") or {}
    if data.get("is_prescribed") or fire.get("is_prescribed"):
        return True
    name = (fire.get("name") or "").lower()
    return "prescribed" in name or "planned" in name


# ---------------------------------------------------------------------------
# threat scoring
# ---------------------------------------------------------------------------

def _threat_factors(
    fire: dict,
    *,
    distance_km: float | None,
    within_km: float,
    acreage_hist: list[tuple[float, float]],
    wind: dict | None,
    near: tuple[float, float] | None,
) -> dict[str, float]:
    """Compute composite threat factors per README formula.

    Returns ``{score, proximity, size, uncontained, growth, growth_rate,
    wind, bearing, planned}`` — score is the final clamped [0,100] value.
    Degrades when wind/growth absent (those multipliers stay at 1.0).
    """
    data = fire.get("data") or {}
    try:
        acres = float(data.get("acreage") or 0)
    except (TypeError, ValueError):
        acres = 0.0

    if distance_km is None or within_km <= 0:
        proximity = 0.3
    else:
        proximity = max(0.0, min(1.0, 1.0 - (distance_km / within_km)))

    size = max(0.0, min(1.0, log10(1.0 + acres) / log10(1.0 + _SIZE_REF)))

    cont = data.get("containment")
    if isinstance(cont, (int, float)):
        uncontained = max(0.0, 1.0 - (float(cont) / 100.0))
    else:
        uncontained = 1.0

    growth_rate = 0.0
    growth_mul = 1.0
    if acreage_hist and len(acreage_hist) >= 2:
        oldest = max(1e-3, float(acreage_hist[0][1]))
        newest = float(acreage_hist[-1][1])
        if newest > oldest:
            growth_rate = (newest - oldest) / oldest
            growth_mul = 1.0 + _GROWTH_GAIN * growth_rate

    wind_mul = 1.0
    bearing_mul = 1.0
    if wind:
        try:
            speed = float(wind.get("speed") or wind.get("mph") or 0)
        except (TypeError, ValueError):
            speed = 0.0
        wind_mul = 1.0 + _WIND_GAIN * speed
        wb = wind.get("bearing")
        lat, lng = fire.get("lat"), fire.get("lng")
        if (
            wb is not None
            and near is not None
            and isinstance(lat, (int, float))
            and isinstance(lng, (int, float))
        ):
            target = _initial_bearing((float(lat), float(lng)), near)
            diff = abs(((float(wb) - target + 540.0) % 360.0) - 180.0)
            bearing_mul = max(0.5, min(1.5, 1.5 - (diff / 180.0)))

    base = 100.0 * proximity * (0.4 + 0.6 * size) * uncontained
    score = max(0.0, min(100.0, base * growth_mul * wind_mul * bearing_mul))

    planned = _is_planned(fire)
    if planned:
        score = min(score, 5.0)

    return {
        "score": score,
        "proximity": proximity,
        "size": size,
        "uncontained": uncontained,
        "growth": growth_mul,
        "growth_rate": growth_rate,
        "wind": wind_mul,
        "bearing": bearing_mul,
        "planned": 1.0 if planned else 0.0,
    }


def _threat_tier(score: float | None) -> str:
    """Tier role name: `red` / `amber` / `green` / `dimmer`."""
    if score is None:
        return "dimmer"
    if score >= 60:
        return "red"
    if score >= 20:
        return "amber"
    if score < 6:
        return "dimmer"
    return "green"


def _threat_bar_glyphs(score: float | None) -> str:
    """3-segment ▰▱ bar string."""
    if score is None:
        return _THREAT_EMPTY * 3
    s = max(0.0, min(100.0, float(score)))
    if s >= 60:
        n = 3
    elif s >= 30:
        n = 2
    elif s >= 10:
        n = 1
    else:
        n = 0
    return _THREAT_FULL * n + _THREAT_EMPTY * (3 - n)


# ---------------------------------------------------------------------------
# state + worker
# ---------------------------------------------------------------------------

@dataclass
class _TuiState:
    """All mutable UI state. Owned by the main (UI) thread."""

    fires: list[dict] = field(default_factory=list)
    distances: dict[int, float] = field(default_factory=dict)
    threat_scores: dict[int, float] = field(default_factory=dict)
    threat_factors: dict[int, dict] = field(default_factory=dict)
    acreage_history: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    distance_history: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    wind: dict[int, dict] = field(default_factory=dict)
    grown_fire_ids: set[int] = field(default_factory=set)

    visible_fires: list[dict] = field(default_factory=list)
    reports_cache: dict[int, list[dict]] = field(default_factory=dict)
    radio_cache: dict[int, list[dict]] = field(default_factory=dict)
    cameras_cache: dict[int, list[dict]] = field(default_factory=dict)
    fps_cache: dict[int, list[dict]] = field(default_factory=dict)
    image_cache: dict[str, bytes] = field(default_factory=dict)
    chip_cache: dict[int, list[dict]] = field(default_factory=dict)
    aircraft_catalog: list[dict] = field(default_factory=list)

    selected_idx: int = 0
    list_scroll: int = 0
    detail_scroll: int = 0
    sort_key: str = "threat"
    sort_reverse: bool = False
    filter_text: str = ""
    filter_active: bool = False
    filter_buffer: str = ""
    cmd_active: bool = False
    cmd_buffer: str = ""
    focus: str = _FOCUS_LIST
    active_tab: str = "updates"
    list_compact: bool = False
    image_size: str = "med"   # one of "small" / "med" / "large"
    mouse_wheel_invert: bool = False
    mouse_debug: bool = False
    last_mouse_bstate: int = 0   # for debug: shown in status when invert toggles

    last_refresh_ts: float = 0.0
    refresh_in_flight: bool = False
    status_msg: str = ""
    status_msg_ts: float = 0.0
    status_is_error: bool = False
    pending_requests: set[tuple] = field(default_factory=set)
    loading_reports_for: int | None = None

    types: tuple[str, ...] = GEO_EVENT_TYPES
    near: tuple[float, float] | None = None
    near_source: str = ""
    within_km: float = 250.0
    auto_refresh: int = 0

    quit: bool = False
    last_g: float = 0.0
    last_left: float = 0.0
    live_mode: bool = False
    last_live_poll_ts: float = 0.0
    flash_report_ids: set[int] = field(default_factory=set)

    image_show_for: int | None = None
    image_show_url: str | None = None
    pending_mapscii: tuple[float, float] | None = None
    mapscii_embed: Any = None        # _MapsciiEmbed | None
    mapscii_rect: tuple = ()         # (y0, x0, h, w) where we paint it
    last_scroll_change_ts: float = 0.0
    last_known_detail_scroll: int = 0
    last_prefetch_for: int | None = None
    bulk_prefetched: bool = False
    header_image_url: str | None = None
    header_image_last_fire: int | None = None
    header_image_last_paint_ts: float = 0.0
    header_image_enabled: bool = True
    update_image_slots: list = field(default_factory=list)
    update_image_pending: list = field(default_factory=list)
    update_image_painted: set = field(default_factory=set)

    last_drawn_fire_id: int | None = None
    last_drawn_tab: str = ""
    last_drawn_detail_scroll: int = 0
    tab_rects: list = field(default_factory=list)
    detail_scroll_max: int = 0
    list_scroll_max: int = 0


def _worker_loop(
    client: WatchDutyClient,
    req_q: "queue.Queue[tuple]",
    res_q: "queue.Queue[tuple]",
    stop_event: threading.Event,
) -> None:
    """Background fetcher — drains ``req_q``, posts to ``res_q``."""
    while not stop_event.is_set():
        try:
            req = req_q.get(timeout=0.2)
        except queue.Empty:
            continue
        if req is None:
            return
        kind = req[0]
        try:
            if kind == _REQ_REFRESH_FIRES:
                _, types, active_only = req
                fires = client.list_geo_events(types=types, active_only=active_only)
                res_q.put(("FIRES", fires))
            elif kind == _REQ_LOAD_REPORTS:
                _, fire_id = req
                reports = list(client.iter_reports(fire_id, page_size=_REPORTS_RENDER_LIMIT))
                res_q.put(("REPORTS", fire_id, reports))
            elif kind == _REQ_LOAD_RADIO:
                _, fire_id, lat, lng = req
                feeds = client.list_radio_feeds(lat, lng) or []
                res_q.put(("RADIO", fire_id, feeds))
            elif kind == _REQ_LOAD_CAMS:
                _, fire_id, lat, lng = req
                cams = client.list_cameras(lat, lng) or []
                res_q.put(("CAMS", fire_id, cams))
            elif kind == _REQ_LOAD_FPS:
                _, fire_id = req
                runs = client.fps_runs(fire_id) or []
                res_q.put(("FPS", fire_id, runs))
            elif kind == _REQ_LOAD_AIRCRAFT:
                catalog = _aircraft.load_catalog(client)
                res_q.put(("AIRCRAFT", catalog))
            elif kind == _REQ_FETCH_IMAGE:
                _, fire_id, url = req
                data = client.fetch_camera_image(url)
                res_q.put(("IMAGE", fire_id, url, data))
        except WatchDutyError as e:
            res_q.put(("ERROR", kind, req, str(e)))
        except Exception as e:  # noqa: BLE001
            res_q.put(("ERROR", kind, req, f"{type(e).__name__}: {e}"))


# ---------------------------------------------------------------------------
# derived data
# ---------------------------------------------------------------------------

def _recompute_distances(state: _TuiState) -> None:
    """Refill ``state.distances`` from ``state.fires`` + ``state.near``."""
    state.distances.clear()
    if state.near is None:
        return
    for e in state.fires:
        eid = e.get("id")
        lat, lng = e.get("lat"), e.get("lng")
        if eid is None or lat is None or lng is None:
            continue
        state.distances[int(eid)] = _haversine_km(
            state.near, (float(lat), float(lng))
        )


def _record_histories(state: _TuiState) -> None:
    """Append the current acreage + distance to each fire's history."""
    now = time.time()
    for e in state.fires:
        eid = e.get("id")
        if eid is None:
            continue
        eid = int(eid)
        d = e.get("data") or {}
        try:
            acres = float(d.get("acreage") or 0)
        except (TypeError, ValueError):
            acres = 0.0
        h = state.acreage_history.setdefault(eid, [])
        if not h or h[-1][1] != acres:
            h.append((now, acres))
            del h[:-_HISTORY_LIMIT]
        dist = state.distances.get(eid)
        if dist is not None:
            dh = state.distance_history.setdefault(eid, [])
            if not dh or abs(dh[-1][1] - dist) > 0.01:
                dh.append((now, dist))
                del dh[:-_HISTORY_LIMIT]


def _recompute_threats(state: _TuiState) -> None:
    """Refill ``state.threat_scores`` + ``threat_factors`` for visible fires."""
    state.threat_scores.clear()
    state.threat_factors.clear()
    state.grown_fire_ids.clear()
    for e in state.fires:
        eid = e.get("id")
        if eid is None:
            continue
        eid_i = int(eid)
        f = _threat_factors(
            e,
            distance_km=state.distances.get(eid_i),
            within_km=state.within_km,
            acreage_hist=state.acreage_history.get(eid_i) or [],
            wind=state.wind.get(eid_i),
            near=state.near,
        )
        state.threat_scores[eid_i] = f["score"]
        state.threat_factors[eid_i] = f
        if f["growth_rate"] >= 0.10:
            state.grown_fire_ids.add(eid_i)


def _recompute_visible(state: _TuiState) -> None:
    """Recompute ``state.visible_fires`` from filter+sort+distance state."""
    src = state.fires
    needle = state.filter_text.strip().lower()

    out: list[dict] = []
    for e in src:
        eid = e.get("id")
        if eid is None:
            continue
        if state.near is not None:
            d = state.distances.get(int(eid))
            if d is None or d > state.within_km:
                continue
        if needle:
            hay = " ".join([
                str(eid),
                str(e.get("name") or ""),
                str(e.get("address") or ""),
            ]).lower()
            if needle not in hay:
                continue
        out.append(e)

    key = state.sort_key
    if key == "distance" and state.near is None:
        key = "updated"
    if key == "threat" and not state.threat_scores:
        key = "updated"

    def keyfn(e: dict) -> Any:
        eid = e.get("id")
        eid_i = int(eid) if eid is not None else -1
        if key == "threat":
            return -state.threat_scores.get(eid_i, 0.0)
        if key == "distance":
            return state.distances.get(eid_i, inf)
        if key == "acreage":
            return -(float((e.get("data") or {}).get("acreage") or 0))
        if key == "updated":
            return e.get("date_modified") or ""
        return 0

    reverse = state.sort_reverse
    if key == "updated" and not state.sort_reverse:
        reverse = True

    out.sort(key=keyfn, reverse=reverse)
    state.visible_fires = out
    state.bulk_prefetched = False
    if state.visible_fires:
        state.selected_idx = max(0, min(state.selected_idx, len(state.visible_fires) - 1))
    else:
        state.selected_idx = 0


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# color
# ---------------------------------------------------------------------------

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
        acres = (e.get("data") or {}).get("acreage")
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

    # Camera frame in top-right; KV block on the left.
    # Dynamic scaling with two bounds:
    #   - width:  ≤ 55% of detail width (KV gets ≥ kv_min cols).
    #   - height: ≤ 50% of body height minus the title rows.
    # Whichever bound is tighter wins; the other dimension follows the
    # 16:9 aspect (chars are ~2:1, so cam_h ≈ cam_w * 0.32).
    kv_min = 36
    aspect = 0.32
    cam_w = 0
    cam_h = 0
    cam_y = y
    upper_avail = max(0, upper_max - title_h)
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
            cam_w = cam_h = 0
    kv_w = width - cam_w - (2 if cam_w else 0)
    cam_x = x0 + kv_w + 2
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
    from . import images as _img_mod
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
    for r_km, ring_radius_frac in (
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
        for i, (g, n, sub) in enumerate(legend_rows[:plot_h - 1]):
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


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------

def _enqueue_refresh(state: _TuiState, req_q: "queue.Queue[tuple]") -> None:
    key = (_REQ_REFRESH_FIRES,)
    if key in state.pending_requests:
        return
    state.pending_requests.add(key)
    state.refresh_in_flight = True
    req_q.put((_REQ_REFRESH_FIRES, state.types, True))


def _enqueue_reports(state: _TuiState, req_q: "queue.Queue[tuple]", fire_id: int) -> None:
    key = (_REQ_LOAD_REPORTS, fire_id)
    if key in state.pending_requests:
        return
    state.pending_requests.add(key)
    state.loading_reports_for = fire_id
    req_q.put((_REQ_LOAD_REPORTS, fire_id))


def _enqueue_fps(state: _TuiState, req_q: "queue.Queue[tuple]", fire_id: int) -> None:
    if fire_id in state.fps_cache:
        return
    key = (_REQ_LOAD_FPS, fire_id)
    if key in state.pending_requests:
        return
    state.pending_requests.add(key)
    req_q.put((_REQ_LOAD_FPS, fire_id))


def _enqueue_aircraft_catalog(state: _TuiState, req_q: "queue.Queue[tuple]") -> None:
    if state.aircraft_catalog:
        return
    key = (_REQ_LOAD_AIRCRAFT,)
    if key in state.pending_requests:
        return
    state.pending_requests.add(key)
    req_q.put((_REQ_LOAD_AIRCRAFT,))


def _enqueue_image(
    state: _TuiState, req_q: "queue.Queue[tuple]", fire_id: int, url: str,
) -> None:
    if url in state.image_cache:
        return
    key = (_REQ_FETCH_IMAGE, fire_id, url)
    if key in state.pending_requests:
        return
    state.pending_requests.add(key)
    req_q.put((_REQ_FETCH_IMAGE, fire_id, url))


def _enqueue_side_internal(
    state: _TuiState, req_q: "queue.Queue[tuple]",
    eid: int, lat: float, lng: float, kind: str,
) -> None:
    if kind == _REQ_LOAD_RADIO and eid in state.radio_cache:
        return
    if kind == _REQ_LOAD_CAMS and eid in state.cameras_cache:
        return
    key = (kind, eid)
    if key in state.pending_requests:
        return
    state.pending_requests.add(key)
    req_q.put((kind, eid, lat, lng))


def _bulk_prefetch_visible(state: _TuiState, req_q: "queue.Queue[tuple]") -> None:
    if state.bulk_prefetched:
        return
    rows = state.visible_fires
    if not rows or len(rows) > _BULK_PREFETCH_THRESHOLD:
        return
    state.bulk_prefetched = True
    for e in rows:
        eid = e.get("id")
        if eid is None:
            continue
        eid_i = int(eid)
        if eid_i not in state.reports_cache:
            _enqueue_reports(state, req_q, eid_i)
        _enqueue_fps(state, req_q, eid_i)
        lat, lng = e.get("lat"), e.get("lng")
        if lat is not None and lng is not None:
            _enqueue_side_internal(state, req_q, eid_i,
                                   float(lat), float(lng), _REQ_LOAD_RADIO)
            _enqueue_side_internal(state, req_q, eid_i,
                                   float(lat), float(lng), _REQ_LOAD_CAMS)
    _set_status(state, f"prefetching {len(rows)} fires…")


def _prefetch_for_selection(
    state: _TuiState, req_q: "queue.Queue[tuple]", fire: dict,
) -> None:
    """Prefetch on selection change.

    Selected fire gets the full bundle (reports + radio + cams + fps).
    Fires in the ``±_NEIGHBOR_REPORT_WINDOW`` window get reports only so
    j/k navigation reads from cache.
    """
    eid = fire.get("id")
    lat, lng = fire.get("lat"), fire.get("lng")
    if eid is None:
        return
    eid = int(eid)
    if state.last_prefetch_for == eid:
        return
    state.last_prefetch_for = eid
    if eid not in state.reports_cache:
        _enqueue_reports(state, req_q, eid)
    _enqueue_fps(state, req_q, eid)
    if lat is not None and lng is not None:
        _enqueue_side_internal(state, req_q, eid, float(lat), float(lng),
                               _REQ_LOAD_RADIO)
        _enqueue_side_internal(state, req_q, eid, float(lat), float(lng),
                               _REQ_LOAD_CAMS)
    # Neighborhood reports.
    if state.visible_fires:
        try:
            ci = next(
                i for i, e in enumerate(state.visible_fires)
                if e.get("id") == eid
            )
        except StopIteration:
            return
        for off in range(1, _NEIGHBOR_REPORT_WINDOW + 1):
            for ni in (ci - off, ci + off):
                if 0 <= ni < len(state.visible_fires):
                    ne = state.visible_fires[ni]
                    neid = ne.get("id")
                    if (neid is not None
                            and int(neid) not in state.reports_cache):
                        _enqueue_reports(state, req_q, int(neid))


def _set_status(state: _TuiState, msg: str, is_error: bool = False) -> None:
    state.status_msg = msg
    state.status_msg_ts = time.monotonic()
    state.status_is_error = is_error


def _jump_to_next_match(state: _TuiState, direction: int) -> None:
    if not state.filter_text or not state.visible_fires:
        return
    n = len(state.visible_fires)
    start = state.selected_idx
    needle = state.filter_text.lower()
    for off in range(1, n + 1):
        i = (start + direction * off) % n
        e = state.visible_fires[i]
        hay = " ".join([str(e.get("id") or ""), str(e.get("name") or ""),
                        str(e.get("address") or "")]).lower()
        if needle in hay:
            state.selected_idx = i
            return


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
                from .location import detect_location
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
        state.filter_active = False
        state.filter_buffer = state.filter_text
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
    pushback: list[int] = []

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
    if btn == 64:   # wheel up
        if state.mouse_wheel_invert:
            btn = 65
    if btn == 65:
        if state.mouse_wheel_invert:
            btn = 64
    if btn == 64 and is_press:
        if over_list:
            state.selected_idx = max(0, state.selected_idx - 1)
        else:
            state.detail_scroll = max(0, state.detail_scroll - step)
    elif btn == 65 and is_press:
        if over_list and state.visible_fires:
            state.selected_idx = min(len(state.visible_fires) - 1,
                                     state.selected_idx + 1)
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
                    state.selected_idx = new_idx
        elif layout.show_detail:
            state.focus = _FOCUS_DETAIL
    return -1   # swallowed


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

    if wheel_up_mask and (bstate & wheel_up_mask):
        if over_list:
            state.selected_idx = max(0, state.selected_idx - 1)
        else:
            state.detail_scroll = max(0, state.detail_scroll - step)
        return
    if wheel_down_mask and (bstate & wheel_down_mask):
        if over_list and state.visible_fires:
            state.selected_idx = min(len(state.visible_fires) - 1,
                                     state.selected_idx + 1)
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
                state.selected_idx = new_idx
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
            state.selected_idx = 0
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
            state.selected_idx = min(len(rows) - 1, state.selected_idx + 1)
        return True
    if ch in (ord("k"), curses.KEY_UP):
        if state.focus == _FOCUS_DETAIL:
            state.detail_scroll = max(0, state.detail_scroll - 1)
            return True
        if rows:
            state.selected_idx = max(0, state.selected_idx - 1)
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
            state.selected_idx = len(rows) - 1
        return True
    if ch == 4:  # Ctrl-D
        if rows:
            state.selected_idx = min(len(rows) - 1,
                                     state.selected_idx + height // 2)
        return True
    if ch == 21:  # Ctrl-U
        if rows:
            state.selected_idx = max(0, state.selected_idx - height // 2)
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
        from . import images as _img
        if not _img.supports_inline_images(sys.stdout):
            term = os.environ.get("TERM", "")
            tp = os.environ.get("TERM_PROGRAM", "")
            _set_status(state,
                        f"terminal lacks inline images "
                        f"(TERM={term} TERM_PROGRAM={tp}); needs kitty/ghostty/iTerm2",
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


# ---------------------------------------------------------------------------
# result draining (history + flash + notifications)
# ---------------------------------------------------------------------------

def _drain_results(state: _TuiState, res_q: "queue.Queue[tuple]") -> bool:
    changed = False
    while True:
        try:
            msg = res_q.get_nowait()
        except queue.Empty:
            break
        changed = True
        kind = msg[0]
        if kind == "FIRES":
            _, fires = msg
            state.fires = fires or []
            state.last_refresh_ts = time.monotonic()
            state.refresh_in_flight = False
            state.pending_requests.discard((_REQ_REFRESH_FIRES,))
            _recompute_distances(state)
            _record_histories(state)
            _recompute_threats(state)
            _recompute_visible(state)
            state.bulk_prefetched = False
        elif kind == "REPORTS":
            _, fire_id, reports = msg
            old = state.reports_cache.get(int(fire_id)) or []
            old_ids = {int(r["id"]) for r in old if isinstance(r.get("id"), int)}
            new_ids = {int(r["id"]) for r in reports if isinstance(r.get("id"), int)}
            fresh = new_ids - old_ids
            if old_ids and fresh:
                state.flash_report_ids = fresh
                _set_status(state, f"+{len(fresh)} new update"
                                   f"{'s' if len(fresh) != 1 else ''}")
                _notify_new_updates(state, fire_id, reports, fresh)
            state.reports_cache[int(fire_id)] = reports
            # FIFO cap so long sessions don't grow unbounded. Never evict the
            # selected fire's entry.
            if len(state.reports_cache) > _REPORTS_CACHE_MAX:
                cur_eid = None
                if state.visible_fires:
                    cf = state.visible_fires[state.selected_idx].get("id")
                    if cf is not None:
                        cur_eid = int(cf)
                for k in list(state.reports_cache.keys()):
                    if len(state.reports_cache) <= _REPORTS_CACHE_MAX:
                        break
                    if k == cur_eid or k == int(fire_id):
                        continue
                    del state.reports_cache[k]
            state.pending_requests.discard((_REQ_LOAD_REPORTS, int(fire_id)))
            if state.loading_reports_for == fire_id:
                state.loading_reports_for = None
        elif kind == "RADIO":
            _, fire_id, feeds = msg
            state.radio_cache[int(fire_id)] = feeds
            state.pending_requests.discard((_REQ_LOAD_RADIO, int(fire_id)))
        elif kind == "CAMS":
            _, fire_id, cams = msg
            state.cameras_cache[int(fire_id)] = cams
            state.pending_requests.discard((_REQ_LOAD_CAMS, int(fire_id)))
        elif kind == "FPS":
            _, fire_id, runs = msg
            state.fps_cache[int(fire_id)] = runs
            state.pending_requests.discard((_REQ_LOAD_FPS, int(fire_id)))
        elif kind == "AIRCRAFT":
            _, catalog = msg
            state.aircraft_catalog = catalog
            state.pending_requests.discard((_REQ_LOAD_AIRCRAFT,))
        elif kind == "IMAGE":
            _, fire_id, url, data = msg
            state.image_cache[url] = data
            state.pending_requests.discard((_REQ_FETCH_IMAGE, int(fire_id), url))
        elif kind == "ERROR":
            _, req_kind, req, errmsg = msg
            _set_status(state, f"error: {errmsg}", is_error=True)
            if req_kind == _REQ_REFRESH_FIRES:
                state.refresh_in_flight = False
                state.pending_requests.discard((_REQ_REFRESH_FIRES,))
            elif req_kind == _REQ_LOAD_REPORTS:
                fid = req[1]
                state.pending_requests.discard((_REQ_LOAD_REPORTS, fid))
                if state.loading_reports_for == fid:
                    state.loading_reports_for = None
            elif req_kind in (_REQ_LOAD_RADIO, _REQ_LOAD_CAMS):
                fid = req[1]
                state.pending_requests.discard((req_kind, fid))
            elif req_kind == _REQ_LOAD_FPS:
                fid = req[1]
                state.pending_requests.discard((_REQ_LOAD_FPS, fid))
            elif req_kind == _REQ_LOAD_AIRCRAFT:
                state.pending_requests.discard((_REQ_LOAD_AIRCRAFT,))
            elif req_kind == _REQ_FETCH_IMAGE:
                fid, url = req[1], req[2]
                state.pending_requests.discard((_REQ_FETCH_IMAGE, fid, url))
                state.image_show_for = None
                state.image_show_url = None
    return changed


# ---------------------------------------------------------------------------
# image + camera helpers (kept from previous build)
# ---------------------------------------------------------------------------

def _pick_image_url(state: _TuiState, fire: dict) -> str | None:
    """Pick the best image URL for ``fire`` (report media → nearest cam)."""
    eid = fire.get("id")
    if eid is None:
        return None
    eid = int(eid)
    reports = state.reports_cache.get(eid) or []
    for r in reports:
        for m in r.get("media") or []:
            url = (m.get("url") or m.get("thumbnail_url")) \
                if isinstance(m, dict) else None
            if url:
                return url
    cams = state.cameras_cache.get(eid) or []
    if not cams:
        return None
    lat, lng = fire.get("lat"), fire.get("lng")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        def _km(cam: dict) -> float:
            ll = cam.get("latlng") or {}
            a, b = ll.get("lat"), ll.get("lng")
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return _haversine_km((float(lat), float(lng)),
                                     (float(a), float(b)))
            return inf
        cams = sorted(cams, key=_km)
    for cam in cams:
        u = cam.get("image_url")
        if u:
            return u
    return None


def _ensure_header_image(state: _TuiState, req_q: "queue.Queue[tuple]", fire: dict) -> None:
    eid = fire.get("id")
    if eid is None:
        state.header_image_url = None
        return
    url = _pick_image_url(state, fire)
    state.header_image_url = url
    if url and url not in state.image_cache:
        _enqueue_image(state, req_q, int(eid), url)


def _nearby_cameras(cams: list[dict], lat: float, lng: float,
                    radius_km: float = 50.0) -> list[dict]:
    out: list[tuple[float, dict]] = []
    for cam in cams or []:
        ll = cam.get("latlng") or {}
        a, b = ll.get("lat"), ll.get("lng")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        d = _haversine_km((float(lat), float(lng)), (float(a), float(b)))
        if d <= radius_km:
            out.append((d, cam))
    out.sort(key=lambda t: t[0])
    return [c for _, c in out]


def _paint_header_image(stdscr, state: _TuiState, layout: _Layout) -> None:
    """Blit the live-camera bytes inside the detail-pane camera frame."""
    if not state.header_image_enabled:
        return
    if not state.visible_fires or not layout.show_detail:
        return
    from . import images as _img
    if not _img.supports_inline_images(sys.stdout):
        return
    url = state.header_image_url
    if not url or url not in state.image_cache:
        return
    data = state.image_cache[url]
    if not data:
        return
    e = state.visible_fires[state.selected_idx]
    eid = e.get("id")
    if eid is None:
        return
    eid_i = int(eid)
    if (state.header_image_last_fire == eid_i
            and time.monotonic() - state.header_image_last_paint_ts < 3.0):
        return
    width = layout.detail_w - _DETAIL_PAD_X
    kv_min = 36
    aspect = 0.32
    body_h = layout.body_bot - layout.body_top
    # Match _draw_detail's bounds exactly.
    tab_bar_h = 2
    title_h = 6
    content_min = max(8, body_h // 2)
    upper_max = max(6, body_h - content_min - tab_bar_h)
    upper_avail = max(0, upper_max - title_h)
    cam_w = 0
    cam_h = 0
    if width >= 60 and upper_avail >= 4:
        w_cap = max(0, min(width - kv_min, int(width * 0.55)))
        h_cap = upper_avail
        h_from_w = max(4, int(w_cap * aspect))
        if h_from_w <= h_cap:
            cam_w, cam_h = w_cap, h_from_w
        else:
            cam_h = h_cap
            cam_w = min(w_cap, max(20, int(cam_h / aspect)))
    if cam_w < 20 or cam_h < 4:
        return
    cam_x = layout.list_w + _DETAIL_PAD_X + (width - cam_w)
    # Title block is 6 rows: subheader · ▄ · name · ▀ · url · spacer.
    cam_y = layout.body_top + 6
    escape = _img.render_inline(
        data, max_cols=cam_w - 2, max_rows=cam_h - 2, stream=sys.stdout,
    )
    if not escape:
        return
    try:
        sys.stdout.write(f"\x1b[{cam_y + 2};{cam_x + 2}H")
        sys.stdout.write(escape)
        sys.stdout.flush()
    except (OSError, ValueError):
        return
    state.header_image_last_fire = eid_i
    state.header_image_last_paint_ts = time.monotonic()


def _paint_update_images(stdscr, state: _TuiState) -> None:
    """Blit per-update thumbnails. Dedupes by (rid,url,y,x,w,h) so a
    redraw with the same slot positions doesn't re-blit (avoids the
    visible flash when scrolling on iTerm/kitty). Also debounces painting
    while the user is actively scrolling — re-blitting on every tick
    while detail_scroll changes is the actual strobe source."""
    if not state.update_image_slots:
        state.update_image_painted = set()
        return
    # If scroll was changed in the last 120ms, defer the paint so we
    # don't blit a new image every key-repeat.
    if (state.last_scroll_change_ts
            and time.monotonic() - state.last_scroll_change_ts < 0.12):
        return
    from . import images as _img
    if not _img.supports_inline_images(sys.stdout):
        return
    painted_now: set = set()
    for slot in state.update_image_slots:
        try:
            rid, y, x, url, max_w, max_h = slot
        except (ValueError, TypeError):
            continue
        data = state.image_cache.get(url)
        if not data:
            continue
        # Position-aware key: if scroll moves the slot, the key changes
        # and we repaint. If neither the URL nor the position changed we
        # short-circuit so the image stays put without re-blitting.
        key = (rid, url, y, x, max_w, max_h)
        if key in state.update_image_painted:
            painted_now.add(key)
            continue
        escape = _img.render_inline(
            data, max_cols=max_w, max_rows=max_h, stream=sys.stdout,
        )
        if not escape:
            continue
        try:
            sys.stdout.write(f"\x1b[{y + 1};{x + 1}H")
            sys.stdout.write(escape)
            sys.stdout.flush()
        except (OSError, ValueError):
            continue
        painted_now.add(key)
    state.update_image_painted = painted_now


def _clear_inline_images(stdscr, state: _TuiState) -> None:
    """Drop any placed kitty images + reset throttle bookkeeping."""
    from . import images as _img
    try:
        if _img.supports_kitty(sys.stdout):
            sys.stdout.write("\x1b_Ga=d\x1b\\")
            sys.stdout.flush()
    except (OSError, ValueError):
        pass
    state.header_image_last_fire = None
    state.header_image_last_paint_ts = 0.0
    state.update_image_painted = set()
    state.update_image_slots = []


def _notify_new_updates(
    state: _TuiState, fire_id: int, reports: list, fresh: set,
) -> None:
    """Audible bell + toast for fresh reports — drives escalation alerting."""
    if not fresh:
        return
    fire_name = "?"
    for e in state.visible_fires:
        if e.get("id") == fire_id:
            fire_name = e.get("name") or "?"
            break
    title = "Watch Duty"
    body = f"{len(fresh)} new update{'s' if len(fresh) != 1 else ''} on {fire_name}"
    try:
        sys.stdout.write("\a")
        sys.stdout.write(f"\x1b]9;{body}\x07")
        sys.stdout.write(f"\x1b]777;notify;{title};{body}\x07")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Embedded mapscii (PTY hosted, paints into the Map tab rectangle)
# ---------------------------------------------------------------------------

_ANSI_CUP_RE = re.compile(rb"\x1b\[(\d*);?(\d*)H")
_ANSI_HVP_RE = re.compile(rb"\x1b\[(\d*);?(\d*)f")
_ANSI_FULL_CLEAR_RE = re.compile(rb"\x1b\[2J")
_ANSI_HOME_RE = re.compile(rb"\x1b\[H")


_MAPSCII_FOOTER_RE = re.compile(
    r"center:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"
    r".*?zoom:\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _mercator_pixel(lat: float, lng: float, zoom: float) -> tuple[float, float]:
    """Web-Mercator world pixel for (lat, lng) at the given fractional zoom.

    Mapscii draws tiles at 256 px per tile-edge, so this returns pixels in
    the same coordinate system mapscii uses internally — letting us compute
    the cell offset between the map's current centre and the fire point.
    """
    n = 2.0 ** float(zoom)
    world_px = 256.0 * n
    px = (float(lng) + 180.0) / 360.0 * world_px
    sl = max(-0.9999, min(0.9999, sin(radians(float(lat)))))
    py = (0.5 - log((1.0 + sl) / (1.0 - sl)) / (4.0 * pi)) * world_px
    return px, py


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


class _MapsciiEmbed:
    """Hosts a mapscii process in a background PTY and emulates a VT
    inside the Map-tab rectangle using `pyte`. Each tick we drain the
    PTY into pyte's virtual screen, then paint the visible cells into
    curses so the result composes correctly with the rest of the
    dashboard (no flicker, no escape-rewriting fragility).
    """

    def __init__(self, binary: str, lat: float, lng: float, zoom: int,
                 rows: int, cols: int):
        self.binary = binary
        self.fire_lat = float(lat)
        self.fire_lng = float(lng)
        self.fire_key: tuple[float, float, int] = (
            round(lat, 5), round(lng, 5), int(zoom),
        )
        self.rows = max(8, rows)
        self.cols = max(20, cols)
        self.pid = -1
        self.fd = -1
        self.alive = False
        self.screen = None
        self.stream = None
        try:
            import pyte  # type: ignore
        except ImportError:
            self.unavailable = (
                "embedded mapscii needs `pyte` — "
                "install with `pip install libwatchduty[tui]`"
            )
            return
        self.unavailable = None
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.ByteStream(self.screen)
        self._spawn(lat, lng, zoom)

    def _spawn(self, lat: float, lng: float, zoom: int) -> None:
        try:
            pid, fd = pty.fork()
        except OSError:
            return
        if pid == 0:
            # child
            os.environ["TERM"] = "xterm-256color"
            os.environ["LINES"] = str(self.rows)
            os.environ["COLUMNS"] = str(self.cols)
            os.environ["MAPSCII_LAT"] = f"{lat:.5f}"
            os.environ["MAPSCII_LNG"] = f"{lng:.5f}"
            os.environ["MAPSCII_ZOOM"] = str(int(zoom))
            try:
                os.execvp(self.binary, [self.binary])
            except OSError:
                os._exit(127)
        self.pid = pid
        self.fd = fd
        self.alive = True
        try:
            ws = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, ws)
            fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        except OSError:
            pass

    def matches(self, lat: float, lng: float, zoom: int) -> bool:
        return (self.fire_key == (round(lat, 5), round(lng, 5), int(zoom))
                and self.alive)

    def resize(self, rows: int, cols: int) -> None:
        rows = max(8, rows)
        cols = max(20, cols)
        if (rows, cols) == (self.rows, self.cols):
            return
        self.rows, self.cols = rows, cols
        if self.screen:
            try:
                # pyte preserves the old cell contents on resize, which
                # leaves a ghost of the previous frame in the new
                # rectangle until mapscii rerenders. Reset clears the
                # buffer so we paint whitespace until the next frame
                # arrives — far less jarring than seeing scrambled tiles.
                self.screen.resize(rows, cols)
                self.screen.reset()
                self.screen.resize(rows, cols)
            except Exception:
                pass
        try:
            ws = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, ws)
            os.kill(self.pid, signal.SIGWINCH)
            # Nudge mapscii's render pipeline — its SIGWINCH handler
            # re-draws, but sending a no-op cursor key prompts another
            # frame so the new size catches even if SIGWINCH is debounced
            # internally.
            os.write(self.fd, b"\x1b[C\x1b[D")
        except OSError:
            pass

    def poll(self) -> bool:
        """Feed any new PTY bytes into pyte's screen."""
        if not self.alive or not self.stream:
            return False
        got = False
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                self.alive = False
                return got
            if not data:
                self.alive = False
                break
            try:
                self.stream.feed(data)
            except Exception:
                pass
            got = True
        return got

    def send(self, data: bytes) -> None:
        """Write keystrokes to mapscii (arrow keys, a/z, q etc.)."""
        if not self.alive:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def _current_center(self) -> tuple[float, float, float] | None:
        """Parse mapscii's footer for ``(lat, lng, zoom)``. Returns None
        when the footer isn't on screen yet (e.g. tiles still loading)."""
        if not self.screen:
            return None
        try:
            display = self.screen.display
        except Exception:
            return None
        # Footer is the last row but mapscii also writes a notification
        # on the first row — scan from the bottom up for the first match.
        for sy in range(len(display) - 1, -1, -1):
            row = display[sy]
            m = _MAPSCII_FOOTER_RE.search(row)
            if m:
                try:
                    return float(m.group(1)), float(m.group(2)), float(m.group(3))
                except ValueError:
                    return None
        return None

    def _fire_cell(self) -> tuple[int, int] | None:
        """Where in our (cols, rows) cell grid the fire should be drawn,
        or None if it lies outside the visible viewport."""
        c = self._current_center()
        if c is None:
            # Footer not parsed yet → assume mapscii is still on the
            # initial frame for `(fire_lat, fire_lng)`; centre is the
            # fire itself.
            return self.cols // 2, self.rows // 2
        clat, clng, czoom = c
        fpx, fpy = _mercator_pixel(self.fire_lat, self.fire_lng, czoom)
        cpx, cpy = _mercator_pixel(clat, clng, czoom)
        # Braille glyph = 2 horizontal × 4 vertical pixels per cell.
        dx_cell = int(round((fpx - cpx) / 2.0))
        dy_cell = int(round((fpy - cpy) / 4.0))
        sx = self.cols // 2 + dx_cell
        sy = self.rows // 2 + dy_cell
        # Leave the footer row alone.
        if 0 <= sx < self.cols and 0 <= sy < max(1, self.rows - 1):
            return sx, sy
        return None

    def paint(self, stdscr, y0: int, x0: int, holder: dict) -> None:
        """Paint pyte's virtual screen into curses cells at (y0, x0),
        then overlay a fire marker at the projected cell."""
        if not self.screen:
            return
        try:
            buf = self.screen.buffer
        except Exception:
            return
        for sy in range(self.rows):
            row = buf[sy]
            for sx in range(self.cols):
                cell = row[sx]
                ch = cell.data or " "
                if not ch:
                    ch = " "
                fg = _pyte_color_index(cell.fg)
                bg = _pyte_color_index(cell.bg)
                attr = _embed_pair_attr(fg, bg)
                if cell.bold:
                    attr |= curses.A_BOLD
                if cell.reverse:
                    attr |= curses.A_REVERSE
                _addnstr(stdscr, y0 + sy, x0 + sx, ch[:1], 1, attr)
        # Fire marker on top.
        fc = self._fire_cell()
        if fc is not None:
            sx, sy = fc
            marker_attr = (
                _embed_pair_attr(_pyte_color_index("red"), -1)
                | curses.A_BOLD | curses.A_REVERSE
            )
            _addnstr(stdscr, y0 + sy, x0 + sx, "▲", 1, marker_attr)

    def close(self) -> None:
        if self.pid > 0:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
            self.pid = -1
        if self.fd > 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self.alive = False


def _bundled_mapscii() -> str | None:
    """Return the in-repo (or wheel-shared) mapscii binary path if any.

    Checked locations, in order:
      1. ``$LIBWATCHDUTY_MAPSCII`` env override
      2. Editable / sdist checkout: ``<repo>/vendor/mapscii``
      3. Wheel-installed shared data: ``<sys.prefix>/share/libwatchduty/vendor/mapscii``
    """
    env = os.environ.get("LIBWATCHDUTY_MAPSCII")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env

    here = os.path.dirname(os.path.abspath(__file__))
    candidates: list[str] = []
    for rel in (
        os.path.join("..", "..", "..", "vendor", "mapscii"),
        os.path.join("..", "..", "vendor", "mapscii"),
        os.path.join("..", "vendor", "mapscii"),
    ):
        candidates.append(os.path.normpath(
            os.path.join(here, rel, "node_modules", ".bin", "mapscii")
        ))
    # Wheel-installed shared data path.
    candidates.append(os.path.join(
        sys.prefix, "share", "libwatchduty", "vendor", "mapscii",
        "node_modules", ".bin", "mapscii",
    ))
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _show_mapscii(stdscr, lat: float, lng: float, zoom: int = 13) -> str | None:
    """Suspend curses, shell out to `mapscii` for an interactive map view.

    Resolution order: bundled vendor/mapscii binary first, then `mapscii`
    on $PATH. Returns None on success, or an error string when neither
    is available / launch fails.
    """
    binary = _bundled_mapscii() or shutil.which("mapscii")
    if not binary:
        return (
            "mapscii not found — run `watchduty-install-mapscii` "
            "(needs node+npm)"
        )
    try:
        curses.def_prog_mode()
        curses.endwin()
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
        env = dict(os.environ)
        # Upstream mapscii main.js ignores `-l`; our vendored copy reads
        # these env vars and recenters after init.
        env["MAPSCII_LAT"] = f"{lat:.5f}"
        env["MAPSCII_LNG"] = f"{lng:.5f}"
        env["MAPSCII_ZOOM"] = str(int(zoom))
        try:
            subprocess.run(
                [binary, "-l", f"{lat:.5f},{lng:.5f},{int(zoom)}"],
                env=env, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return f"mapscii failed: {type(e).__name__}: {e}"
    finally:
        try:
            curses.reset_prog_mode()
            stdscr.clear()
            stdscr.refresh()
        except curses.error:
            pass
    return None


def _show_image_preview(stdscr, data: bytes) -> None:
    """Leave curses, render image fullscreen, wait for a key, return."""
    from . import images as _img
    if not data or not _img.supports_inline_images(sys.stdout):
        return
    rows, _ = stdscr.getmaxyx()
    try:
        curses.def_prog_mode()
        curses.endwin()
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.write(_img.render_inline(
            data, max_rows=max(8, rows - 2), stream=sys.stdout,
        ))
        sys.stdout.write("\n[press any key to return to the dashboard]\n")
        sys.stdout.flush()
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            try:
                sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            sys.stdin.read(1)
    finally:
        try:
            curses.reset_prog_mode()
            stdscr.clear()
            stdscr.refresh()
        except curses.error:
            pass


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def _app(stdscr, client: WatchDutyClient, state: _TuiState) -> int:
    """Run the curses event loop."""
    curses.curs_set(0)
    stdscr.timeout(_TICK_MS)
    stdscr.keypad(True)
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass

    holder: dict = {}
    _init_colors(holder)

    try:
        # ALL_MOUSE_EVENTS so every variant (press/release/click/wheel)
        # bubbles up — older curses builds report wheel as BUTTON4/5_RELEASED
        # instead of PRESSED, and we want both.
        avail_mask, _ = curses.mousemask(
            getattr(curses, "ALL_MOUSE_EVENTS", -1)
        )
        _ = bool(avail_mask)
    except (curses.error, AttributeError):
        pass
    # Try to enable SGR-extended mouse mode. ncurses 6+ does this itself
    # but some terminals (iTerm / ghostty) need the explicit toggle. Any
    # raw SGR bytes that leak past curses are consumed by the dispatch
    # parser below (`_drain_sgr_mouse`).
    try:
        sys.stdout.write("\x1b[?1006h")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass

    req_q: "queue.Queue[tuple]" = queue.Queue()
    res_q: "queue.Queue[tuple]" = queue.Queue()
    stop_event = threading.Event()
    worker = threading.Thread(
        target=_worker_loop, args=(client, req_q, res_q, stop_event),
        daemon=True,
    )
    worker.start()

    resized = {"flag": False}

    def _on_winch(_sig, _frm):
        resized["flag"] = True

    try:
        signal.signal(signal.SIGWINCH, _on_winch)
    except (AttributeError, ValueError):
        pass

    _enqueue_refresh(state, req_q)
    _enqueue_aircraft_catalog(state, req_q)
    _set_status(state, "loading fires…")

    show_help = False
    last_dims = stdscr.getmaxyx()

    try:
        while not state.quit:
            try:
                cur_dims = stdscr.getmaxyx()
                if resized["flag"] or cur_dims != last_dims:
                    curses.update_lines_cols()
                    _clear_inline_images(stdscr, state)
                    # Re-clamp all scroll positions to the new geometry —
                    # they get re-derived from the next draw, but the
                    # ceilings (`detail_scroll_max`, `list_scroll_max`)
                    # haven't been recomputed yet. Floor at 0, leave the
                    # upper clamp to the draw fns once they re-measure.
                    state.detail_scroll = max(0, state.detail_scroll)
                    state.list_scroll = max(0, state.list_scroll)
                    if state.visible_fires:
                        state.selected_idx = max(0, min(
                            state.selected_idx,
                            len(state.visible_fires) - 1,
                        ))
                    # Resize the embedded mapscii pty to match the new
                    # layout — it sends SIGWINCH to mapscii and the next
                    # draw re-allocates the pyte buffer.
                    if state.mapscii_embed is not None and state.mapscii_rect:
                        # Geometry is recomputed in _draw_detail; here we
                        # just nudge mapscii to repaint at the new size.
                        try:
                            os.kill(state.mapscii_embed.pid, signal.SIGWINCH)
                        except (OSError, AttributeError):
                            pass
                    # Force a full redraw of everything (lists, tab panel,
                    # KV, etc.) since pane widths/heights may have shifted.
                    state.last_drawn_fire_id = None
                    state.last_drawn_tab = ""
                    stdscr.clear()
                    last_dims = cur_dims
                    resized["flag"] = False

                _drain_results(state, res_q)

                if state.image_show_for is not None and state.image_show_url:
                    url = state.image_show_url
                    if url in state.image_cache:
                        data = state.image_cache[url]
                        if data:
                            _show_image_preview(stdscr, data)
                            _set_status(state,
                                        f"shown image ({len(data)//1024} KB)")
                        else:
                            _set_status(state,
                                        f"image fetch returned 0 bytes for {url[-40:]}",
                                        is_error=True)
                        state.image_show_for = None
                        state.image_show_url = None

                if state.pending_mapscii is not None:
                    lat, lng = state.pending_mapscii
                    state.pending_mapscii = None
                    _clear_inline_images(stdscr, state)
                    err = _show_mapscii(stdscr, lat, lng, zoom=13)
                    if err:
                        _set_status(state, err, is_error=True)
                    else:
                        _set_status(state, "back from mapscii")
                    resized["flag"] = True   # force a full repaint

                if state.auto_refresh and state.last_refresh_ts \
                        and not state.refresh_in_flight:
                    if time.monotonic() - state.last_refresh_ts >= state.auto_refresh:
                        _enqueue_refresh(state, req_q)

                if state.live_mode and state.visible_fires:
                    if time.monotonic() - state.last_live_poll_ts >= _LIVE_POLL_SECONDS:
                        e_live = state.visible_fires[state.selected_idx]
                        eid_live = e_live.get("id")
                        if eid_live is not None:
                            key_live = (_REQ_LOAD_REPORTS, int(eid_live))
                            if key_live not in state.pending_requests:
                                state.pending_requests.add(key_live)
                                req_q.put((_REQ_LOAD_REPORTS, int(eid_live)))
                                state.last_live_poll_ts = time.monotonic()

                lines, cols = stdscr.getmaxyx()
                layout = _compute_layout(lines, cols)

                cur_fire_id: int | None = None
                if state.visible_fires:
                    cf = state.visible_fires[state.selected_idx].get("id")
                    if cf is not None:
                        cur_fire_id = int(cf)
                # Only wipe placed images when the user navigates to a
                # different fire or tab. Plain scroll keeps existing
                # placements and lets the painter's per-slot dedupe handle
                # the repaint quietly — avoids the "image flash" on j/k.
                if (cur_fire_id != state.last_drawn_fire_id
                        or state.active_tab != state.last_drawn_tab):
                    _clear_inline_images(stdscr, state)
                    stdscr.clear()
                    # Kill the embedded mapscii when leaving the tab or
                    # changing fire — saves a long-running node process
                    # and frees the PTY.
                    if (state.active_tab != "map"
                            and state.mapscii_embed is not None):
                        state.mapscii_embed.close()
                        state.mapscii_embed = None
                        state.mapscii_rect = ()
                state.last_drawn_fire_id = cur_fire_id
                state.last_drawn_tab = state.active_tab
                state.last_drawn_detail_scroll = state.detail_scroll

                stdscr.erase()
                if layout.too_small:
                    msg = f"terminal too small (need {_MIN_COLS}x{_MIN_LINES})"
                    _addnstr(stdscr, 0, 0, msg, cols,
                             _attr("error", holder))
                else:
                    _draw_header(stdscr, state, layout, holder)
                    _draw_list(stdscr, state, layout, holder)
                    _draw_detail(stdscr, state, layout, holder)
                    _draw_footer(stdscr, state, layout, holder)
                stdscr.noutrefresh()

                if show_help:
                    _draw_help_overlay(stdscr, layout, holder)

                curses.doupdate()

                if not show_help and not layout.too_small:
                    _paint_header_image(stdscr, state, layout)
                    _paint_update_images(stdscr, state)

                if state.update_image_pending:
                    for rid, url in state.update_image_pending:
                        _enqueue_image(state, req_q, rid, url)
                    state.update_image_pending.clear()

                if state.status_msg == "__HELP__":
                    show_help = True
                    state.status_msg = ""
                    state.status_msg_ts = 0

                try:
                    ch = stdscr.getch()
                except curses.error:
                    ch = -1
                except KeyboardInterrupt:
                    state.quit = True
                    continue

                # If curses (or our manual escape) is delivering SGR mouse
                # events as raw bytes — `ESC [ < <btn> ; <x> ; <y> M|m` —
                # consume the full sequence and dispatch via our own
                # parser, instead of letting `[`/`<`/digits leak through
                # to keybinds (which previously made all fires vanish).
                ch = _maybe_consume_sgr_mouse(
                    stdscr, ch, state, req_q, layout,
                )

                if show_help:
                    if ch != -1 and ch != curses.KEY_RESIZE:
                        show_help = False
                    if ch == curses.KEY_RESIZE:
                        resized["flag"] = True
                    continue

                if ch == curses.KEY_RESIZE:
                    resized["flag"] = True
                    continue

                if ch != -1:
                    _handle_key(state, req_q, layout, ch)

                # Note scroll deltas so _paint_update_images can debounce.
                if state.detail_scroll != state.last_known_detail_scroll:
                    state.last_scroll_change_ts = time.monotonic()
                    state.last_known_detail_scroll = state.detail_scroll

                if state.focus == _FOCUS_DETAIL and state.visible_fires:
                    e = state.visible_fires[state.selected_idx]
                    eid = e.get("id")
                    if eid is not None and int(eid) not in state.reports_cache:
                        _enqueue_reports(state, req_q, int(eid))

                if state.visible_fires:
                    _prefetch_for_selection(
                        state, req_q, state.visible_fires[state.selected_idx],
                    )

                _bulk_prefetch_visible(state, req_q)

                if state.visible_fires:
                    _ensure_header_image(
                        state, req_q,
                        state.visible_fires[state.selected_idx],
                    )

            except KeyboardInterrupt:
                state.quit = True
            except curses.error:
                continue
    finally:
        try:
            sys.stdout.write("\x1b[?1006l")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        if state.mapscii_embed is not None:
            try:
                state.mapscii_embed.close()
            except Exception:
                pass
            state.mapscii_embed = None
        stop_event.set()
        try:
            req_q.put_nowait(None)
        except Exception:
            pass
        worker.join(timeout=2.0)

    return 0


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def run(
    token: str | None = None,
    near: tuple[float, float] | None = None,
    types: list[str] | None = None,
    auto_refresh: int = 0,
    *,
    within_km: float = 250.0,
    near_source: str = "",
) -> int:
    """Launch the interactive curses TUI.

    Returns the process exit code suitable for ``sys.exit(...)``.
    """
    if not sys.stdout.isatty():
        print("tui requires a tty; try `watchduty fires`", file=sys.stderr)
        return 2

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    state = _TuiState()
    state.types = tuple(types) if types else GEO_EVENT_TYPES
    state.near = near
    state.near_source = near_source
    state.within_km = float(within_km)
    state.auto_refresh = (
        max(_MIN_AUTO_REFRESH, int(auto_refresh)) if auto_refresh else 0
    )
    # Default sort: threat when --near is set; updated otherwise.
    state.sort_key = "threat" if near is not None else "updated"

    client = WatchDutyClient(token=token)

    try:
        return curses.wrapper(_app, client, state)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        try:
            sys.stderr.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(run())
