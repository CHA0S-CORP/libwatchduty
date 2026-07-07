"""Background worker (thread-pooled fetch loop) + request enqueue helpers."""

from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from .. import aircraft as _aircraft
from ..client import WatchDutyClient, WatchDutyError
from .derive import _set_status
from .state import (
    _BULK_PREFETCH_THRESHOLD,
    _NEIGHBOR_REPORT_WINDOW,
    _REPORTS_RENDER_LIMIT,
    _TuiState,
)


# Worker-request kinds.
_REQ_REFRESH_FIRES = "REFRESH_FIRES"
_REQ_LOAD_REPORTS = "LOAD_REPORTS"
_REQ_LOAD_RADIO = "LOAD_RADIO"
_REQ_LOAD_CAMS = "LOAD_CAMS"
_REQ_LOAD_FPS = "LOAD_FPS"
_REQ_LOAD_AIRCRAFT = "LOAD_AIRCRAFT"
_REQ_FETCH_IMAGE = "FETCH_IMAGE"


def _handle_request(
    client: WatchDutyClient,
    res_q: "queue.Queue[tuple]",
    req: tuple,
) -> None:
    """Run one request against the client and post its result to ``res_q``."""
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


def _worker_loop(
    client: WatchDutyClient,
    req_q: "queue.Queue[tuple]",
    res_q: "queue.Queue[tuple]",
    stop_event: threading.Event,
) -> None:
    """Background fetcher — drains ``req_q``, posts to ``res_q``.

    Each request is farmed out to a small thread pool so one slow image
    fetch can't stall every other request queued behind it. The ``None``
    sentinel still drains and returns; the pool is shut down (without
    waiting) on the way out.
    """
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        while not stop_event.is_set():
            try:
                req = req_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if req is None:
                return
            pool.submit(_handle_request, client, res_q, req)
    finally:
        pool.shutdown(wait=False)


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
