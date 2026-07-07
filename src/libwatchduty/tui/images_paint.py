"""Inline-image plumbing: URL picking, header/update blits, notifications."""

from __future__ import annotations

import curses
import queue
import sys
import time
from math import inf

from .helpers import _haversine_km
from .layout import _Layout
from .state import _TuiState
from .worker import _enqueue_image


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
    from .. import images as _img
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
    # Match _draw_detail's bounds exactly — geometry shared via
    # _camera_rect (title block is 6 rows: subheader · ▄ · name · ▀ ·
    # url · spacer). Imported lazily to avoid a module-import cycle.
    from .draw_detail import _camera_rect
    rect = _camera_rect(layout, title_h=6)
    if rect is None:
        return
    cam_y, cam_x, cam_h, cam_w = rect
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
    from .. import images as _img
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
    from .. import images as _img
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
    """Audible bell + toast for fresh reports — drives escalation alerting.

    The escapes are queued on ``state.pending_stdout`` and flushed by the
    app loop right after ``curses.doupdate()`` so raw stdout writes can't
    race the curses frame.
    """
    if not fresh:
        return
    fire_name = "?"
    for e in state.visible_fires:
        if e.get("id") == fire_id:
            fire_name = e.get("name") or "?"
            break
    title = "Watch Duty"
    body = f"{len(fresh)} new update{'s' if len(fresh) != 1 else ''} on {fire_name}"
    state.pending_stdout.append(
        "\a" + f"\x1b]9;{body}\x07" + f"\x1b]777;notify;{title};{body}\x07"
    )


def _show_image_preview(stdscr, data: bytes) -> None:
    """Leave curses, render image fullscreen, wait for a key, return."""
    from .. import images as _img
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
