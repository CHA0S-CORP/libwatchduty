"""Derived data: distances, histories, threat scores, visible set, selection."""

from __future__ import annotations

import time
from math import inf
from typing import Any

from .helpers import _haversine_km, _threat_factors
from .state import _HISTORY_LIMIT, _TuiState


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
    # ▲-growing threshold. v1's growth_rate is a relative fraction
    # (0.10 = +10% acreage); v2's is absolute acres/hour.
    grow_thresh = 0.10 if state.threat_model == "v1" else 10.0
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
            model=state.threat_model,
        )
        state.threat_scores[eid_i] = f["score"]
        state.threat_factors[eid_i] = f
        if f["growth_rate"] >= grow_thresh:
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
    # Keep the SAME FIRE selected across re-filters / re-sorts when it is
    # still visible; otherwise clamp the row index as before and re-anchor
    # ``selected_fire_id`` from the row that ends up selected.
    if state.selected_fire_id is not None:
        for i, e in enumerate(out):
            eid = e.get("id")
            if eid is not None and int(eid) == state.selected_fire_id:
                state.selected_idx = i
                return
    if state.visible_fires:
        state.selected_idx = max(0, min(state.selected_idx, len(state.visible_fires) - 1))
        eid = state.visible_fires[state.selected_idx].get("id")
        state.selected_fire_id = int(eid) if eid is not None else None
    else:
        state.selected_idx = 0
        state.selected_fire_id = None


def _select_idx(state: _TuiState, idx: int) -> None:
    """Set ``selected_idx`` and re-anchor ``selected_fire_id`` to that row.

    Every direct selection change (j/k, G/gg, mouse, match jump) goes
    through here so a later refresh + re-sort keeps the same fire selected.
    """
    state.selected_idx = idx
    if 0 <= idx < len(state.visible_fires):
        eid = state.visible_fires[idx].get("id")
        state.selected_fire_id = int(eid) if eid is not None else None
    else:
        state.selected_fire_id = None


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
            _select_idx(state, i)
            return
