"""Capture a still frame from the camera closest to a fire.

Demonstrates:
  - Listing cameras near a point (`list_cameras(lat, lng)`).
  - Sorting by haversine distance and picking the closest one with a
    live `image_url`.
  - Downloading the still bytes via `fetch_camera_image(url)` — which
    uses a CDN-safe header set (some image hosts 403 on the regular
    API session headers).

Run:
    python examples/camera_capture.py 105316 --out fire.jpg
    python examples/camera_capture.py 105316 --radius 25 --provider alertwest
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from libwatchduty import WatchDutyClient, WatchDutyError


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = a
    lat2, lng2 = b
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fire_id", type=int, help="geo_event id to anchor on")
    parser.add_argument("--out", type=Path, default=Path("camera.jpg"),
                        help="output path (default camera.jpg)")
    parser.add_argument("--radius", type=float, default=50.0,
                        help="max camera distance in km (default 50)")
    parser.add_argument("--provider",
                        help="filter cameras by provider (e.g. alertwest)")
    args = parser.parse_args()

    client = WatchDutyClient()
    try:
        ev = client.get_geo_event(args.fire_id)
    except WatchDutyError as e:
        print(f"could not fetch fire #{args.fire_id}: {e}", file=sys.stderr)
        return 1

    lat, lng = ev.get("lat"), ev.get("lng")
    if not (isinstance(lat, (int, float)) and isinstance(lng, (int, float))):
        print(f"fire #{args.fire_id} has no lat/lng", file=sys.stderr)
        return 1
    home = (float(lat), float(lng))

    cams = client.list_cameras(home[0], home[1]) or []
    if args.provider:
        cams = [c for c in cams if c.get("provider") == args.provider]

    # Filter to cameras with a usable image_url and within radius.
    ranked: list[tuple[float, dict]] = []
    for cam in cams:
        ll = cam.get("latlng") or {}
        clat, clng = ll.get("lat"), ll.get("lng")
        if not (isinstance(clat, (int, float)) and isinstance(clng, (int, float))):
            continue
        d = haversine_km(home, (float(clat), float(clng)))
        if d <= args.radius and cam.get("image_url"):
            ranked.append((d, cam))
    ranked.sort(key=lambda r: r[0])
    if not ranked:
        print(f"no cameras within {args.radius:g} km", file=sys.stderr)
        return 2

    dist, cam = ranked[0]
    url = cam["image_url"]
    print(f"closest cam {cam.get('id', '?')} ({cam.get('name', '?')}) at "
          f"{dist:.1f} km — fetching {url}")
    try:
        data = client.fetch_camera_image(url)
    except WatchDutyError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 3
    args.out.write_bytes(data)
    print(f"wrote {len(data):,} bytes → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
