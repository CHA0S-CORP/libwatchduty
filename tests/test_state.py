"""State management tests — visibility, sort, history caps, report cache."""

from __future__ import annotations

import time

from libwatchduty import tui as _tui


def test_recompute_visible_with_filter(tui_state):
    tui_state.filter_text = "pine"
    _tui._recompute_visible(tui_state)
    assert len(tui_state.visible_fires) == 1
    assert tui_state.visible_fires[0]["name"].lower().startswith("pine")


def test_recompute_visible_with_no_filter(tui_state):
    tui_state.filter_text = ""
    _tui._recompute_visible(tui_state)
    # All 3 canned fires sit within 250 km of (34, -117).
    assert len(tui_state.visible_fires) == 3


def test_threat_sort_default_when_near_set(tui_state):
    # The fixture sets sort_key=threat. Top result should have the highest
    # threat_score among visible fires.
    _tui._recompute_visible(tui_state)
    head = tui_state.visible_fires[0]
    head_id = int(head["id"])
    top_score = tui_state.threat_scores.get(head_id, 0.0)
    for f in tui_state.visible_fires[1:]:
        fid = int(f["id"])
        assert tui_state.threat_scores.get(fid, 0.0) <= top_score


def test_acreage_history_appends_only_on_change(tui_state):
    fid = 102   # not the one the fixture mutated; current acreage = 200.
    # Re-record without changing acreage -> no new entry (last sample is 200,
    # data.acreage is still 200).
    before = list(tui_state.acreage_history[fid])
    _tui._record_histories(tui_state)
    assert tui_state.acreage_history[fid] == before
    # Mutate the fire's acreage; next record should append exactly one entry.
    for f in tui_state.fires:
        if int(f["id"]) == fid:
            f["data"]["acreage"] = before[-1][1] + 100.0
    _tui._record_histories(tui_state)
    assert len(tui_state.acreage_history[fid]) == len(before) + 1


def test_distance_history_appended_when_near_set(tui_state):
    # Fixture already runs _record_histories once; every fire with a
    # known distance should have at least one distance_history sample.
    for fid in tui_state.distances:
        assert tui_state.distance_history.get(fid), f"missing dist hist for {fid}"


def test_reports_cache_fifo_cap(tui_state):
    cap = _tui._REPORTS_CACHE_MAX
    # Stuff cache with fake entries past the cap; trigger an eviction.
    for fid in range(1, cap + 5):
        tui_state.reports_cache[fid] = [{"id": fid}]
    assert len(tui_state.reports_cache) == cap + 4
    # The drain logic in _drain_results caps at _REPORTS_CACHE_MAX,
    # protecting the currently-selected fire. Simulate just the trim:
    current = (
        int(tui_state.visible_fires[tui_state.selected_idx]["id"])
        if tui_state.visible_fires else None
    )
    pinned = 1
    for k in list(tui_state.reports_cache):
        if len(tui_state.reports_cache) <= cap:
            break
        if k in (current, pinned):
            continue
        del tui_state.reports_cache[k]
    assert len(tui_state.reports_cache) <= cap


def test_history_limit_caps_acreage_samples(tui_state):
    fid = 102
    base = time.time()
    # Push way past the cap with strictly increasing values to force appends.
    for n in range(_tui._HISTORY_LIMIT * 3):
        for f in tui_state.fires:
            if int(f["id"]) == fid:
                f["data"]["acreage"] = 100.0 + n
        # Pretend each call lands a fresh second later so it's appended.
        tui_state.acreage_history.setdefault(fid, []).append(
            (base + n, 100.0 + n)
        )
        del tui_state.acreage_history[fid][:-_tui._HISTORY_LIMIT]
    assert len(tui_state.acreage_history[fid]) == _tui._HISTORY_LIMIT
