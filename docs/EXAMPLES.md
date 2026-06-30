# Examples

Annotated, runnable scripts living under [`examples/`](../examples/).
Every one is a self-contained module with `if __name__ == "__main__":`
— copy them and tailor freely.

---

## At a glance

| Script | What it does | Why it's interesting |
|---|---|---|
| [`list_fires.py`](../examples/list_fires.py) | Active wildfires (top 25) with acreage + containment | The minimal "did the client work?" probe |
| [`nearest_fires.py`](../examples/nearest_fires.py) | Closest fires to a coordinate, threat-tier glyphs | Mirrors what the TUI's list pane shows — pure-Python sort |
| [`live_watcher.py`](../examples/live_watcher.py) | Polls one fire's report feed; alerts on new updates | Headless equivalent of the TUI's LIVE mode (`L`) |
| [`fire_bundle.py`](../examples/fire_bundle.py) | Dumps event + reports + radio + cams + FPS as JSON | Single call (`get_fire_bundle`), pipes into `jq` |
| [`radio_feeds.py`](../examples/radio_feeds.py) | Lists Broadcastify scanner feeds near a fire or point | Online-first sort, listener counts inline |
| [`camera_capture.py`](../examples/camera_capture.py) | Saves a still from the closest live camera | Uses `fetch_camera_image` (CDN-safe headers) |

Each script declares its own `argparse` interface and a top-of-file
docstring that describes inputs / outputs. Run them with `-h` to see
flags.

---

## Common patterns

### Construct a client

```python
from libwatchduty import WatchDutyClient

# All read endpoints are public; no auth needed.
client = WatchDutyClient()

# For user-scoped endpoints (saved places, profile):
client = WatchDutyClient(token="…")
# or, after a successful login:
client.login("you@example.com", "…")  # stores the DRF token on the session
```

### Inject a session for tests

The constructor takes a `requests.Session`, which is the seam tests
use to wire up [`responses`](https://pypi.org/project/responses/):

```python
import requests, responses
from libwatchduty import WatchDutyClient

with responses.RequestsMock() as rmock:
    rmock.add("GET", "https://api.watchduty.org/api/v1/geo_events/",
              json=[{"id": 1, "name": "Test", "is_active": True}])
    c = WatchDutyClient(session=requests.Session())
    assert c.list_geo_events()[0]["id"] == 1
```

### Paginate reports

The `/reports/` endpoint paginates via `limit` / `offset`.
`iter_reports(fire_id)` walks pages until the server returns a short
one, yielding every report in order:

```python
for r in client.iter_reports(105316, page_size=50):
    print(r["date_created"], r["message"][:80])
```

### Respect the API

`api.watchduty.org` is a small public service. The module-level
constants in [`tui.py`](../src/libwatchduty/tui.py) encode the
minimums we hold ourselves to:

| Constraint | Value | Where |
|---|---|---|
| Auto-refresh floor | 30 s | `_MIN_AUTO_REFRESH` |
| LIVE poll cadence | 30 s | `_LIVE_POLL_SECONDS` |
| Reports per page | 40 | `_REPORTS_RENDER_LIMIT` |
| Per-request timeout | (5 s connect, 20 s read) | `WatchDutyClient(timeout=…)` |
| Retries on 5xx / transport | 3 with 0.5/1/2 s backoff | `WatchDutyClient(retries=…)` |

If you build something on top of the client, default to the same
budget. Tighten on your end, never loosen on the API's.

### Errors

Every endpoint raises [`WatchDutyError`](../src/libwatchduty/client.py)
on non-2xx. It carries the status code + parsed body so you can
branch cleanly:

```python
from libwatchduty import WatchDutyClient, WatchDutyError

try:
    client.get_geo_event(999_999_999)
except WatchDutyError as e:
    if e.status == 404:
        ...
    else:
        raise
```

The transport layer raises `WatchDutyError` with `status=None` for
connect / read / chunked-encoding errors *after* exhausting retries —
treat them the same way you'd treat a 5xx.

---

## Running the examples

```bash
# from a clean clone, in a venv:
pip install -e '.'

python examples/list_fires.py
python examples/nearest_fires.py 33.92 -117.24 --radius 100
python examples/fire_bundle.py 105316 | jq .event.name
python examples/live_watcher.py 105316 --notify
python examples/camera_capture.py 105316 --out cam.jpg
python examples/radio_feeds.py --fire 105316 --online-only
```

For a fully interactive view of the same data — threat-ranked,
live-updating, with a real map — see [`docs/QUICKSTART.md`](QUICKSTART.md)
for the `watchduty tui` walkthrough.
