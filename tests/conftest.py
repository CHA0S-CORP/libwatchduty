"""Shared fixtures for libwatchduty tests.

All fixtures are offline — no real network is ever touched.
The `mock_client` is a duck-typed stand-in for ``WatchDutyClient`` that
returns canned static data covering the methods the TUI calls.
"""

from __future__ import annotations

import time
from typing import Iterator

import pytest

from libwatchduty import tui as _tui


# ---------------------------------------------------------------------------
# canned data
# ---------------------------------------------------------------------------

# A tiny valid PNG (1x1 transparent). Used for fetch_camera_image returns
# so the TUI's image pipeline gets real bytes without any network I/O.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_fire(
    fid: int,
    name: str,
    lat: float,
    lng: float,
    *,
    acreage: float = 500.0,
    containment: float | None = 25.0,
    is_active: bool = True,
    address: str = "",
) -> dict:
    return {
        "id": fid,
        "name": name,
        "lat": lat,
        "lng": lng,
        "address": address or f"{name} area",
        "is_active": is_active,
        "date_modified": "2026-06-29T12:00:00Z",
        "data": {"acreage": acreage, "containment": containment},
    }


_FIRES = [
    _make_fire(101, "Oak Fire",   34.10, -117.05, acreage=1500.0, containment=10.0),
    _make_fire(102, "Pine Fire",  33.92, -117.20, acreage=200.0,  containment=80.0),
    _make_fire(103, "Cedar Fire", 34.30, -116.90, acreage=4200.0, containment=None),
]


def _reports_for(fid: int) -> list[dict]:
    return [
        {
            "id": fid * 10 + n,
            "geo_event_id": fid,
            "message": f"<p>Update {n} for fire {fid}.</p>",
            "is_active": True,
            "date_created": "2026-06-29T11:30:00Z",
            "date_modified": "2026-06-29T11:45:00Z",
        }
        for n in range(1, 4)
    ]


_RADIO_FEEDS = [
    {
        "feed_id": 22877,
        "name": "Test County Fire / EMS",
        "description": "Dispatch and tactical",
        "online": True,
        "listeners": 42,
        "listen_url": "https://example.invalid/listen.mp3",
    },
]


_CAMERAS = [
    {
        "id": "cam-1",
        "name": "Test Lookout",
        "provider": "alertwest",
        "latlng": {"lat": 34.05, "lng": -117.10},
        "image_url": "https://example.invalid/cam1.jpg",
        "is_offline": False,
    },
]


_FPS_RUNS = [
    {"id": 1, "run_at": "2026-06-29T11:00:00Z", "status": "completed"},
]


_AIRCRAFT_CATALOG = [
    {"tail": "N123WD", "type": "AT-802F", "role": "tanker"},
]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _MockClient:
    """Duck-typed WatchDutyClient with canned returns.

    Only the methods the TUI calls are implemented. Each method just
    yields the static fixture data above so tests stay offline.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((name, args, kwargs))

    def list_geo_events(self, *args, **kwargs) -> list[dict]:
        self._record("list_geo_events", args, kwargs)
        return [dict(f) for f in _FIRES]

    def iter_reports(self, fire_id: int, **kwargs) -> Iterator[dict]:
        self._record("iter_reports", (fire_id,), kwargs)
        for r in _reports_for(int(fire_id)):
            yield r

    def list_radio_feeds(self, lat: float, lng: float) -> list[dict]:
        self._record("list_radio_feeds", (lat, lng), {})
        return [dict(f) for f in _RADIO_FEEDS]

    def list_cameras(self, lat: float | None = None, lng: float | None = None) -> list[dict]:
        self._record("list_cameras", (lat, lng), {})
        return [dict(c) for c in _CAMERAS]

    def fps_runs(self, fire_id: int) -> list[dict]:
        self._record("fps_runs", (fire_id,), {})
        return [dict(r) for r in _FPS_RUNS]

    def list_aircraft(self) -> list[dict]:
        self._record("list_aircraft", (), {})
        return [dict(a) for a in _AIRCRAFT_CATALOG]

    def fetch_camera_image(self, url: str, *, timeout: float | None = None) -> bytes:
        self._record("fetch_camera_image", (url,), {"timeout": timeout})
        return _TINY_PNG


@pytest.fixture
def mock_client() -> _MockClient:
    """Stand-in for WatchDutyClient — canned returns, no network."""
    return _MockClient()


@pytest.fixture
def tui_state(mock_client: _MockClient) -> _tui._TuiState:
    """A `_TuiState` already populated from the mock_client data,
    with `near=(34.0,-117.0)` and `within_km=250`.

    Derived state (distances, threats, visible_fires, histories) has been
    recomputed once so tests can immediately exercise selection/sort/key
    handling without having to reach for the worker thread.
    """
    state = _tui._TuiState()
    state.near = (34.0, -117.0)
    state.near_source = "tests"
    state.within_km = 250.0
    state.sort_key = "threat"

    state.fires = mock_client.list_geo_events()
    _tui._recompute_distances(state)
    _tui._record_histories(state)
    # Force a second snapshot so growth math has 2 points for fire 101.
    state.acreage_history[101].append((time.time() + 1.0, 1800.0))
    _tui._recompute_threats(state)
    _tui._recompute_visible(state)
    return state
