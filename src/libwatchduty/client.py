"""Watch Duty API client.

Reverse-engineered from the browser app at https://app.watchduty.org.
Endpoint base: https://api.watchduty.org/api/v1

Cache-busting `ts` query param is added only to the GET endpoints the
browser app cache-busts (list_geo_events, get_geo_event, get_reports,
evac_zone_statuses); all other GETs go through unmodified.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Iterator

import requests

API_BASE = "https://api.watchduty.org/api/v1"
APP_ORIGIN = "https://app.watchduty.org"
APP_VERSION = "2026.6.18"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

GEO_EVENT_TYPES = ("wildfire", "location", "flooding", "hazard")

_RETRYABLE_STATUS = (500, 502, 503, 504)
_RETRYABLE_EXC = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


class WatchDutyError(RuntimeError):
    """Raised when the Watch Duty API returns a non-2xx response.

    Attributes:
        status: HTTP status code (None if request never completed).
        body: Parsed JSON body if available, else raw text (None on transport failure).
        method: HTTP method of the failed request, if known.
        url: Full URL of the failed request, if known.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: Any = None,
        *,
        method: str | None = None,
        url: str | None = None,
    ):
        if method and url and method not in message:
            message = f"{method} {url} -> {message}"
        super().__init__(message)
        self.status = status
        self.body = body
        self.method = method
        self.url = url


class WatchDutyClient:
    """HTTP client for the Watch Duty REST API.

    Most endpoints are public; user-scoped ones (get_user, get_places) require
    a token via the `token` kwarg or set_token(). Wraps a single requests.Session.
    Not thread-safe for header mutation (set_token/clear_token); concurrent GETs
    on a single client are otherwise fine.
    """

    def __init__(
        self,
        base_url: str = API_BASE,
        *,
        token: str | None = None,
        user_agent: str = DEFAULT_UA,
        timeout: float | tuple[float, float] = (5.0, 20.0),
        session: requests.Session | None = None,
        retries: int = 3,
    ):
        """Build a client.

        Args:
            base_url: API root, defaults to production.
            token: If set, sent as `Authorization: Token <token>` on every request.
            user_agent: UA header value, defaults to a Chrome-on-macOS string.
            timeout: Per-request timeout in seconds. Either a single float
                (applied to both connect and read) or a (connect, read) tuple.
            session: Reuse an existing requests.Session; one is created if omitted.
            retries: Max attempts per request on connection errors / 5xx (default 3).
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en",
            "User-Agent": user_agent,
            "Origin": APP_ORIGIN,
            "Referer": APP_ORIGIN + "/",
            "x-app-version": APP_VERSION,
            "x-git-tag": APP_VERSION,
            "x-app-is-native": "false",
        })
        if token:
            self.set_token(token)

    def __repr__(self) -> str:
        authed = "Authorization" in self.session.headers
        return f"WatchDutyClient(base_url={self.base_url!r}, authed={authed})"

    # auth -----------------------------------------------------------------

    def set_token(self, token: str) -> None:
        """Set `Authorization: Token <token>` on subsequent requests.

        Uses the DRF TokenAuthentication scheme. Do NOT use `Bearer`.
        """
        self.session.headers["Authorization"] = f"Token {token}"

    def clear_token(self) -> None:
        """Remove the Authorization header so subsequent requests are unauthenticated."""
        self.session.headers.pop("Authorization", None)

    def sso_required(self, email: str) -> dict:
        """Check whether an email address must authenticate via organization SSO.

        Returns the raw API dict (typically contains an `sso_required` boolean and
        provider info).
        """
        return self._get("/organization/sso/required/", params={"email": email})

    def login(self, username: str, password: str) -> dict:
        """POST /auth/login/. Stores returned token for subsequent calls.

        Returns the raw response dict so callers can inspect user info.
        Side effect: on success, sets the Authorization header on this client.
        """
        resp = self._request(
            "POST",
            "/auth/login/",
            json={"username": username, "password": password},
        )
        if not isinstance(resp, dict):
            raise WatchDutyError(
                "login: unexpected response shape", None, resp,
                method="POST", url=self.base_url + "/auth/login/",
            )
        token = resp.get("key") or resp.get("token") or resp.get("auth_token")
        if token:
            self.set_token(token)
        return resp

    # geo events -----------------------------------------------------------

    def list_geo_events(
        self,
        types: Iterable[str] = GEO_EVENT_TYPES,
        *,
        active_only: bool = False,
    ) -> list[dict]:
        """List geo events (wildfires, locations, flooding, hazards).

        Args:
            types: Iterable of event-type strings; see GEO_EVENT_TYPES for defaults.
            active_only: If True, filter client-side to events with is_active=True.

        Returns the full list of event dicts.
        """
        params = {"geo_event_types": ",".join(types), "ts": _cache_ts()}
        events = self._get("/geo_events/", params=params)
        if active_only:
            events = [e for e in events if e.get("is_active")]
        return events

    def get_geo_event(self, geo_event_id: int) -> dict:
        """Return the full geo event dict for `geo_event_id`. Raises WatchDutyError on 404."""
        return self._get(
            f"/geo_events/{geo_event_id}/", params={"ts": _cache_ts()}
        )

    def list_geo_events_modified_since(
        self,
        modified_since: str,
        types: Iterable[str] = GEO_EVENT_TYPES,
    ) -> list[dict]:
        """List geo events whose date_modified >= `modified_since` (YYYY-MM-DD or ISO-8601).

        Useful for incremental sync. Sends modified_since to the server and also
        filters client-side on each event's date_modified for a guaranteed contract.
        """
        params: dict[str, Any] = {
            "modified_since": modified_since,
            "geo_event_types": ",".join(types),
            "ts": _cache_ts(),
        }
        events = self._get("/geo_events/", params=params) or []
        cutoff = modified_since
        return [
            e for e in events
            if (e.get("date_modified") or "") >= cutoff
        ]

    # reports --------------------------------------------------------------

    def get_reports(
        self,
        geo_event_id: int,
        *,
        status: str = "approved",
        has_lat_lng: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        """Reports (updates) for a geo_event.

        Normalises the upstream response (which may be either a list or a
        `{results: [...]}` dict) to always return a flat list. Use `limit`/`offset`
        for paging, or call `iter_reports` instead.
        """
        params: dict[str, Any] = {
            "geo_event_id": geo_event_id,
            "status": status,
            "ts": _cache_ts(),
        }
        if has_lat_lng is not None:
            params["has_lat_lng"] = "true" if has_lat_lng else "false"
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        r = self._get("/reports/", params=params)
        if isinstance(r, dict) and "results" in r:
            return r["results"]
        return r or []

    def get_report(self, report_id: int) -> dict:
        """Single report (update) by id."""
        return self._get(f"/reports/{report_id}")

    # evac zones -----------------------------------------------------------

    def evac_zone_statuses(self) -> list[dict] | dict:
        """Return current status (e.g. evacuation order/warning) for all known evacuation zones."""
        return self._get("/evac_zones/statuses", params={"ts": _cache_ts()})

    # fire progression models ---------------------------------------------

    def fps_sources(self) -> list[dict]:
        """List available fire progression simulation (FPS) model sources."""
        return self._get("/fire_progression_models/sources/")

    def fps_runs(self, geo_event_id: int) -> list[dict]:
        """List fire progression model runs (predicted spread polygons) for a geo event."""
        return self._get(
            "/fire_progression_models/runs/",
            params={"geo_event_id": geo_event_id},
        )

    def get_fps_run(self, run_id: int) -> dict:
        """Single fire-progression-model run by id (raster/polygon metadata)."""
        return self._get(f"/fire_progression_models/runs/{run_id}")

    # user-scoped ----------------------------------------------------------

    def get_places(self) -> list[dict]:
        """Return the authenticated user's saved places. Requires a token."""
        return self._get("/users/places/")

    def put_places(self, places: list[dict]) -> list[dict]:
        """Replace the authenticated user's saved places with `places`. Requires a token."""
        return self._request("PUT", "/users/places/", json=places)

    def iter_reports(
        self,
        geo_event_id: int,
        *,
        status: str = "approved",
        page_size: int = 50,
    ) -> Iterator[dict]:
        """Yield all reports for a geo_event, paging through with limit/offset.

        Trusts the server to honor `page_size`; if the server silently caps lower,
        iteration exits as soon as a short page is returned.
        """
        offset = 0
        while True:
            items = self.get_reports(
                geo_event_id, status=status, limit=page_size, offset=offset
            )
            if not items:
                return
            for r in items:
                yield r
            if len(items) < page_size:
                return
            offset += page_size

    # broadcastify (radio) -------------------------------------------------

    def list_radio_feeds(self, lat: float, lng: float) -> list[dict]:
        """Broadcastify scanner feeds near a point.

        Each feed: {feed_id, name, description, online, listeners, listen_url}.
        Returns [] when no Broadcastify feeds cover the point.
        """
        return self._get("/broadcastify/", params={"lat": lat, "lng": lng})

    def get_radio_county(self, lat: float, lng: float) -> dict:
        """Broadcastify county-level listen URL for a point: {url, name}."""
        # Path intentionally has no trailing slash — matches the server route.
        return self._get(
            "/broadcastify/county-url", params={"lat": lat, "lng": lng}
        )

    # backwards-compat aliases
    broadcastify = list_radio_feeds
    broadcastify_county_url = get_radio_county

    # cameras --------------------------------------------------------------

    def list_cameras(
        self, lat: float | None = None, lng: float | None = None
    ) -> list[dict]:
        """Wildfire-detection PTZ cameras (AlertCalifornia / AlertWest etc).

        With lat/lng, returns cameras near that point. Without, returns all.
        """
        params: dict[str, Any] = {}
        if lat is not None:
            params["lat"] = lat
        if lng is not None:
            params["lng"] = lng
        return self._get("/cameras/", params=params or None)

    def get_camera(self, camera_id: str | int) -> dict:
        """Return metadata for a single PTZ camera by id (numeric or string)."""
        return self._get(f"/cameras/{camera_id}/")

    def list_cameras_by_provider(self, provider: str) -> list[dict]:
        """Cameras filtered by provider (e.g. "alertwest", "awf").

        Server currently ignores ?provider= and returns the full catalog, so this
        method passes the param through (forward-compat) and filters client-side.
        """
        cams = self._get("/cameras/", params={"provider": provider}) or []
        return [c for c in cams if c.get("provider") == provider]

    def list_cameras_in_bbox(
        self,
        min_lat: float,
        max_lat: float,
        min_lng: float,
        max_lng: float,
    ) -> list[dict]:
        """Cameras within a lat/lng bounding box.

        Server ignores bbox today, so this sends it for forward-compat and
        filters client-side on each camera's latlng.
        """
        bbox = f"{min_lat},{max_lat},{min_lng},{max_lng}"
        cams = self._get("/cameras/", params={"bbox": bbox}) or []
        out = []
        for c in cams:
            ll = c.get("latlng") or {}
            lat_v, lng_v = ll.get("lat"), ll.get("lng")
            if not isinstance(lat_v, (int, float)) or not isinstance(lng_v, (int, float)):
                continue
            if min_lat <= lat_v <= max_lat and min_lng <= lng_v <= max_lng:
                out.append(c)
        return out

    def list_cameras_realtime(self) -> list[dict]:
        """Realtime GIS camera feed. Each item has a `composite_id` like
        `<base64(provider:provider_id)>` plus `latlng`, `image_url`,
        `image_timestamp`, `is_offline`, etc. Used to grab still frames.
        """
        return self._get("/cameras_gis/realtime") or []

    def get_camera_gis(self, composite_id: str) -> dict:
        """Single GIS camera by base64 composite id (e.g. `YWxlcnR3ZXN0OjExMzM5`)."""
        return self._get(f"/cameras_gis/{composite_id}")

    def fetch_camera_image(self, url: str, *, timeout: float | None = None) -> bytes:
        """GET a camera still-frame URL with image-friendly headers.

        Image CDNs (images.watchduty.org, cameras.alertcalifornia.org,
        alertwest.com, etc.) often 403 on the api-style Origin/Referer
        headers carried by the WatchDutyClient session, so this call uses
        an isolated request with just User-Agent + Accept.

        Returns raw bytes. Raises WatchDutyError on non-2xx.
        """
        to = timeout if timeout is not None else self.timeout
        ua = self.session.headers.get("User-Agent", DEFAULT_UA)
        try:
            r = requests.get(
                url,
                timeout=to,
                headers={
                    "User-Agent": ua,
                    "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
                },
                allow_redirects=True,
            )
        except requests.RequestException as e:
            raise WatchDutyError(str(e), None, None, method="GET", url=url) from e
        if not r.ok:
            raise WatchDutyError(
                f"HTTP {r.status_code}", r.status_code, r.text[:500],
                method="GET", url=url,
            )
        return r.content

    # accounts (django-allauth HTML) --------------------------------------

    def get_accounts_page(self) -> str:
        """Fetch the django-allauth catch-all HTML at /accounts/.

        Not a REST endpoint; server typically returns HTTP 406 (raising
        WatchDutyError). Kept for parity with the discovered surface.
        """
        return self._get("/accounts/")

    # current user ---------------------------------------------------------

    def get_user(self) -> dict:
        """Current authenticated user (requires login/token)."""
        return self._get("/auth/user/")

    def list_aircraft(self) -> list[dict]:
        """Global aircraft catalog (1,000+ rows): hex_code, type, classification,
        name, model, tail_num, short_callsign.

        NOT fire-scoped — the API does not expose per-fire aircraft assignments.
        """
        return self._get("/aircraft/") or []

    def get_api_root(self) -> dict:
        """GET the API root; returns `{}` with a token, 401 without.

        Useful only as a cheap auth/connectivity ping.
        """
        return self._get("/")

    # aggregate convenience ------------------------------------------------

    def get_fire_bundle(self, geo_event_id: int) -> dict:
        """Pull everything tied to one fire: details, reports, radio, cams, FPS.

        Always present in the returned dict: `event`, `reports`, `fps_runs`.
        Present only when the event has lat/lng:
        `radio_feeds`, `radio_county`, `cameras`.
        """
        ev = self.get_geo_event(geo_event_id)
        lat, lng = ev.get("lat"), ev.get("lng")
        bundle: dict[str, Any] = {
            "event": ev,
            "reports": list(self.iter_reports(geo_event_id)),
            "fps_runs": self.fps_runs(geo_event_id),
        }
        if lat is not None and lng is not None:
            bundle["radio_feeds"] = self.list_radio_feeds(lat, lng)
            bundle["radio_county"] = self.get_radio_county(lat, lng)
            bundle["cameras"] = self.list_cameras(lat, lng)
        return bundle

    # internals ------------------------------------------------------------

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | tuple[float, float] | None = None,
    ) -> Any:
        return self._request("GET", path, params=params, timeout=timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | tuple[float, float] | None = None,
    ) -> Any:
        """Issue one request with retry-on-5xx/connection-error.

        `params` values should be urlencodable scalars (str|int|float|bool). `json`
        may be any JSON-serializable structure.
        """
        url = self.base_url + path
        eff_timeout = timeout if timeout is not None else self.timeout
        backoffs = (0.5, 1.0, 2.0)
        r: requests.Response | None = None

        for attempt in range(self.retries):
            try:
                r = self.session.request(
                    method, url, params=params, json=json, timeout=eff_timeout
                )
            except _RETRYABLE_EXC as e:
                if attempt + 1 < self.retries:
                    time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
                    continue
                raise WatchDutyError(
                    f"transport error: {type(e).__name__}: {e}",
                    None, None, method=method, url=url,
                ) from e
            except requests.RequestException as e:
                raise WatchDutyError(
                    f"transport error: {type(e).__name__}: {e}",
                    None, None, method=method, url=url,
                ) from e

            if r.status_code in _RETRYABLE_STATUS and attempt + 1 < self.retries:
                time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
                continue
            break

        assert r is not None  # loop guarantees this or raises
        if not r.ok:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            raise WatchDutyError(
                f"HTTP {r.status_code}",
                r.status_code, body, method=method, url=url,
            )
        if r.status_code == 204 or not r.content:
            return None
        ct = r.headers.get("content-type", "")
        if "application/json" in ct:
            return r.json()
        return r.text


def _cache_ts() -> int:
    return int(time.time() * 1000)
