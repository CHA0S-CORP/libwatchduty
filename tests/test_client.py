"""Offline tests for ``libwatchduty.client.WatchDutyClient``.

The HTTP layer is fully mocked (``patch.object`` on ``client.session`` and on
``requests.get`` for camera stills) — no real network is ever touched, and
``time.sleep`` is monkeypatched so retry backoff never actually waits.
"""

from __future__ import annotations

import json as _json
from unittest import mock

import pytest
import requests

from libwatchduty import client as client_mod
from libwatchduty.client import WatchDutyClient, WatchDutyError


# ---------------------------------------------------------------------------
# fakes / fixtures
# ---------------------------------------------------------------------------

_UNSET = object()


class _FakeResponse:
    """Minimal stand-in for requests.Response covering what _request touches."""

    def __init__(
        self,
        status_code: int = 200,
        json_body=_UNSET,
        *,
        text: str | None = None,
        content_type: str = "application/json",
    ):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json_body = json_body
        if json_body is not _UNSET:
            self.text = _json.dumps(json_body)
        else:
            self.text = text or ""
        self.content = self.text.encode()
        self.headers = {"content-type": content_type}

    def json(self):
        if self._json_body is _UNSET:
            raise ValueError("no JSON body")
        return self._json_body


@pytest.fixture
def sleeps(monkeypatch) -> list:
    """Record (and skip) all time.sleep calls made by the client module."""
    calls: list = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: calls.append(s))
    return calls


@pytest.fixture
def client() -> WatchDutyClient:
    """Client with throttling disabled so only backoff sleeps are observed."""
    return WatchDutyClient(min_interval=0)


# ---------------------------------------------------------------------------
# retry behaviour
# ---------------------------------------------------------------------------

def test_retry_on_502_then_success(client, sleeps):
    responses = [_FakeResponse(502, text="bad gateway"), _FakeResponse(json_body={"ok": True})]
    with mock.patch.object(client.session, "request", side_effect=responses) as req:
        assert client.get_api_root() == {"ok": True}
    assert req.call_count == 2
    # exactly one backoff sleep, using the first backoff step.
    assert sleeps == [pytest.approx(0.5)]


def test_retries_exhausted_raises_with_status(client, sleeps):
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(503, text="nope")
    ) as req:
        with pytest.raises(WatchDutyError) as excinfo:
            client.get_api_root()
    assert req.call_count == client.retries == 3
    assert excinfo.value.status == 503
    # sleeps between attempts only (retries-1), following the backoff schedule.
    assert sleeps == [pytest.approx(0.5), pytest.approx(1.0)]


def test_connection_error_retried_then_raises_status_none(client, sleeps):
    with mock.patch.object(
        client.session, "request", side_effect=requests.ConnectionError("boom")
    ) as req:
        with pytest.raises(WatchDutyError) as excinfo:
            client.get_api_root()
    assert req.call_count == 3
    assert excinfo.value.status is None
    assert excinfo.value.body is None
    assert "ConnectionError" in str(excinfo.value)
    assert len(sleeps) == 2


def test_connection_error_then_success(client, sleeps):
    side = [requests.ConnectionError("blip"), _FakeResponse(json_body=[1, 2])]
    with mock.patch.object(client.session, "request", side_effect=side) as req:
        assert client.get_api_root() == [1, 2]
    assert req.call_count == 2
    assert sleeps == [pytest.approx(0.5)]


# ---------------------------------------------------------------------------
# list_geo_events
# ---------------------------------------------------------------------------

def test_list_geo_events_none_body_returns_empty_list(client, sleeps):
    # Regression: a JSON `null` body must normalise to [] not None.
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=None)
    ):
        assert client.list_geo_events() == []


def test_list_geo_events_active_only_filters_client_side(client, sleeps):
    events = [
        {"id": 1, "is_active": True},
        {"id": 2, "is_active": False},
        {"id": 3},  # missing flag -> filtered out
    ]
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=events)
    ) as req:
        got = client.list_geo_events(active_only=True)
    assert [e["id"] for e in got] == [1]
    # and without the flag, everything passes through.
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=events)
    ):
        assert len(client.list_geo_events()) == 3
    params = req.call_args.kwargs["params"]
    assert params["geo_event_types"] == ",".join(client_mod.GEO_EVENT_TYPES)


# ---------------------------------------------------------------------------
# get_reports normalisation
# ---------------------------------------------------------------------------

def test_get_reports_dict_with_results_normalised(client, sleeps):
    body = {"count": 2, "results": [{"id": 1}, {"id": 2}]}
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=body)
    ):
        assert client.get_reports(101) == [{"id": 1}, {"id": 2}]


def test_get_reports_plain_list_passthrough(client, sleeps):
    body = [{"id": 7}]
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=body)
    ):
        assert client.get_reports(101) == [{"id": 7}]


def test_get_reports_none_body_returns_empty_list(client, sleeps):
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=None)
    ):
        assert client.get_reports(101) == []


# ---------------------------------------------------------------------------
# iter_reports pagination
# ---------------------------------------------------------------------------

def test_iter_reports_full_page_then_short_page_stops(client, sleeps):
    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 3}]  # short page -> iteration ends here
    responses = [_FakeResponse(json_body=page1), _FakeResponse(json_body=page2)]
    with mock.patch.object(client.session, "request", side_effect=responses) as req:
        got = list(client.iter_reports(101, page_size=2))
    assert [r["id"] for r in got] == [1, 2, 3]
    assert req.call_count == 2
    # offset increments by page_size between calls.
    offsets = [c.kwargs["params"]["offset"] for c in req.call_args_list]
    limits = [c.kwargs["params"]["limit"] for c in req.call_args_list]
    assert offsets == [0, 2]
    assert limits == [2, 2]


def test_iter_reports_empty_first_page_yields_nothing(client, sleeps):
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=[])
    ) as req:
        assert list(client.iter_reports(101, page_size=50)) == []
    assert req.call_count == 1


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token_field", ["key", "token", "auth_token"])
def test_login_sets_authorization_header(client, sleeps, token_field):
    body = {token_field: "s3cret"}
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=body)
    ) as req:
        resp = client.login("user@example.com", "hunter2")
    assert resp == body
    assert client.session.headers["Authorization"] == "Token s3cret"
    method, url = req.call_args.args
    assert method == "POST"
    assert url.endswith("/auth/login/")
    assert req.call_args.kwargs["json"] == {
        "username": "user@example.com", "password": "hunter2",
    }


def test_login_non_dict_response_raises(client, sleeps):
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(json_body=["weird"])
    ):
        with pytest.raises(WatchDutyError) as excinfo:
            client.login("u", "p")
    assert excinfo.value.body == ["weird"]
    assert "Authorization" not in client.session.headers


# ---------------------------------------------------------------------------
# WatchDutyError message prefixing
# ---------------------------------------------------------------------------

def test_error_message_prefixed_even_when_body_contains_get(client, sleeps):
    # Regression: prefixing must key off startswith("GET "), not a substring
    # check — a body/message mentioning "GET" must not suppress the prefix.
    url = client.base_url + "/"
    resp = _FakeResponse(
        502, text="upstream GET timed out", content_type="text/plain",
    )
    with mock.patch.object(client.session, "request", return_value=resp):
        with pytest.raises(WatchDutyError) as excinfo:
            client.get_api_root()
    assert str(excinfo.value) == f"GET {url} -> HTTP 502"
    assert excinfo.value.body == "upstream GET timed out"


def test_error_message_with_embedded_get_still_prefixed():
    err = WatchDutyError(
        "server said GET failed", 502, method="GET", url="https://x/y",
    )
    assert str(err) == "GET https://x/y -> server said GET failed"


def test_error_message_not_double_prefixed():
    err = WatchDutyError(
        "GET https://x/y -> HTTP 502", 502, method="GET", url="https://x/y",
    )
    assert str(err) == "GET https://x/y -> HTTP 502"


# ---------------------------------------------------------------------------
# fetch_camera_image
# ---------------------------------------------------------------------------

def test_fetch_camera_image_non_2xx_raises_with_status(client, sleeps):
    resp = _FakeResponse(403, text="forbidden", content_type="text/plain")
    with mock.patch.object(client_mod.requests, "get", return_value=resp) as get:
        with pytest.raises(WatchDutyError) as excinfo:
            client.fetch_camera_image("https://images.example/cam.jpg")
    assert excinfo.value.status == 403
    assert excinfo.value.url == "https://images.example/cam.jpg"
    # isolated request — image-friendly Accept, no session Origin/Referer.
    headers = get.call_args.kwargs["headers"]
    assert "image/" in headers["Accept"]
    assert "Origin" not in headers


def test_fetch_camera_image_transport_error_wrapped(client, sleeps):
    with mock.patch.object(
        client_mod.requests, "get", side_effect=requests.ConnectionError("down")
    ):
        with pytest.raises(WatchDutyError) as excinfo:
            client.fetch_camera_image("https://images.example/cam.jpg")
    assert excinfo.value.status is None
    assert isinstance(excinfo.value.__cause__, requests.ConnectionError)


def test_fetch_camera_image_success_returns_bytes(client, sleeps):
    resp = _FakeResponse(200, text="pngbytes", content_type="image/png")
    with mock.patch.object(client_mod.requests, "get", return_value=resp):
        assert client.fetch_camera_image("https://images.example/cam.jpg") == b"pngbytes"


# ---------------------------------------------------------------------------
# rate limiting (_throttle)
# ---------------------------------------------------------------------------

def test_min_interval_spaces_rapid_requests(monkeypatch, sleeps):
    c = WatchDutyClient(min_interval=0.25)
    # Freeze the monotonic clock so both requests "start" at the same instant.
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: 100.0)
    with mock.patch.object(
        c.session, "request", return_value=_FakeResponse(json_body={})
    ):
        c.get_api_root()  # first request: no prior slot, no sleep
        c.get_api_root()  # second request: must wait out min_interval
    assert sleeps == [pytest.approx(0.25)]


def test_min_interval_zero_never_sleeps(sleeps):
    c = WatchDutyClient(min_interval=0)
    with mock.patch.object(
        c.session, "request", return_value=_FakeResponse(json_body={})
    ):
        c.get_api_root()
        c.get_api_root()
    assert sleeps == []


# ---------------------------------------------------------------------------
# response decoding
# ---------------------------------------------------------------------------

def test_204_returns_none(client, sleeps):
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(204)
    ):
        assert client.get_api_root() is None


def test_empty_body_returns_none(client, sleeps):
    with mock.patch.object(
        client.session, "request", return_value=_FakeResponse(200)
    ):
        assert client.get_api_root() is None


def test_non_json_content_type_returns_text(client, sleeps):
    resp = _FakeResponse(200, text="<html>allauth</html>", content_type="text/html")
    with mock.patch.object(client.session, "request", return_value=resp):
        assert client.get_accounts_page() == "<html>allauth</html>"
