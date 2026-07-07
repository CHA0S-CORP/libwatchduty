"""Main curses event loop (result drain + `_app`) and the public `run()`."""

from __future__ import annotations

import curses
import locale
import os
import queue
import signal
import sys
import threading
import time

from ..client import GEO_EVENT_TYPES, WatchDutyClient
from .chrome import _draw_footer, _draw_header, _draw_help_overlay
from .derive import (
    _recompute_distances,
    _recompute_threats,
    _recompute_visible,
    _record_histories,
    _select_idx,
    _set_status,
)
from .draw_detail import _draw_detail
from .draw_list import _draw_list
from .images_paint import (
    _clear_inline_images,
    _ensure_header_image,
    _notify_new_updates,
    _paint_header_image,
    _paint_update_images,
    _show_image_preview,
)
from .input import (
    _handle_key,
    _maybe_consume_bracketed_paste,
    _maybe_consume_sgr_mouse,
)
from .layout import _addnstr, _compute_layout
from .mapscii_embed import _show_mapscii
from .palette import _attr, _init_colors
from .state import (
    _FLASH_TTL,
    _FOCUS_DETAIL,
    _IMAGE_CACHE_MAX,
    _LIVE_POLL_SECONDS,
    _MIN_AUTO_REFRESH,
    _MIN_COLS,
    _MIN_LINES,
    _REPORTS_CACHE_MAX,
    _TICK_MS,
    _TuiState,
)
from .worker import (
    _REQ_FETCH_IMAGE,
    _REQ_LOAD_AIRCRAFT,
    _REQ_LOAD_CAMS,
    _REQ_LOAD_FPS,
    _REQ_LOAD_RADIO,
    _REQ_LOAD_REPORTS,
    _REQ_REFRESH_FIRES,
    _bulk_prefetch_visible,
    _enqueue_aircraft_catalog,
    _enqueue_image,
    _enqueue_refresh,
    _enqueue_reports,
    _prefetch_for_selection,
    _worker_loop,
)


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
                state.flash_report_ts = time.monotonic()
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
            # FIFO cap — entries are raw JPEG bytes; evict oldest first.
            while len(state.image_cache) > _IMAGE_CACHE_MAX:
                state.image_cache.pop(next(iter(state.image_cache)))
            state.pending_requests.discard((_REQ_FETCH_IMAGE, int(fire_id), url))
        elif kind == "ERROR":
            _, req_kind, req, errmsg = msg
            _set_status(state, f"error: {errmsg}", is_error=True)
            if req_kind == _REQ_REFRESH_FIRES:
                state.refresh_in_flight = False
                state.pending_requests.discard((_REQ_REFRESH_FIRES,))
                # Stamp the attempt so the auto-refresh timer retries after
                # the normal interval — without this a failed FIRST fetch
                # (last_refresh_ts still 0) disables auto-refresh forever.
                state.last_refresh_ts = time.monotonic()
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
        # 1006h: SGR-extended mouse; 2004h: bracketed paste so dragged-in
        # files / pasted text arrive wrapped in ESC[200~ … ESC[201~ —
        # which `_maybe_consume_bracketed_paste` drops so it can't fill
        # the filter prompt with garbage.
        sys.stdout.write("\x1b[?1006h\x1b[?2004h")
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
                        _select_idx(state, max(0, min(
                            state.selected_idx,
                            len(state.visible_fires) - 1,
                        )))
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

                # Expire stale "NEW" flash chips.
                if (state.flash_report_ids
                        and time.monotonic() - state.flash_report_ts > _FLASH_TTL):
                    state.flash_report_ids = set()

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

                # Deferred stdout escapes (bell / OSC notifications) —
                # emitted only after curses has flushed its frame so they
                # can't race the draw (same slot as the image blits below).
                if state.pending_stdout:
                    try:
                        for chunk in state.pending_stdout:
                            sys.stdout.write(chunk)
                        sys.stdout.flush()
                    except (OSError, ValueError):
                        pass
                    state.pending_stdout.clear()

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

                # Bracketed paste FIRST — if a drag-drop or paste arrives
                # while we're parsing keys, the whole `ESC[200~ … ESC[201~`
                # envelope is swallowed before any byte reaches a binding.
                ch = _maybe_consume_bracketed_paste(stdscr, ch, state)
                # Then SGR mouse — `ESC [ < <btn> ; <x> ; <y> M|m` is
                # consumed and dispatched via our parser, so `[`/`<`/digits
                # don't leak into keybinds.
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
            sys.stdout.write("\x1b[?1006l\x1b[?2004l")
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
    # Opt into the Candidate A (ISI-anchored) scorer via env var. Anything
    # other than "v2" leaves the legacy v1 behaviour untouched.
    env_model = (os.environ.get("LIBWATCHDUTY_THREAT_MODEL") or "").strip().lower()
    if env_model in ("v1", "v2"):
        state.threat_model = env_model
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
