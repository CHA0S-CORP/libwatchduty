"""Find Broadcastify scanner feeds near a fire (or a point).

Demonstrates:
  - Two ways to point at a location: by fire id (looks up the
    geo_event's lat/lng) or by raw `--latlng`.
  - Sorting feeds with online ones first, then listener count.
  - One-line and full-table output modes.

Run:
    python examples/radio_feeds.py --fire 105316
    python examples/radio_feeds.py --latlng 33.92,-117.24
    python examples/radio_feeds.py --fire 105316 --online-only
"""

from __future__ import annotations

import argparse
import sys

from libwatchduty import WatchDutyClient, WatchDutyError


def _resolve_latlng(args, client: WatchDutyClient) -> tuple[float, float]:
    """Either parse `--latlng a,b` or look up the fire's coords."""
    if args.latlng:
        try:
            a, b = (v.strip() for v in args.latlng.split(","))
            return float(a), float(b)
        except ValueError as e:
            raise SystemExit(f"bad --latlng: {args.latlng!r}") from e
    ev = client.get_geo_event(args.fire)
    lat, lng = ev.get("lat"), ev.get("lng")
    if not (isinstance(lat, (int, float)) and isinstance(lng, (int, float))):
        raise SystemExit(f"fire #{args.fire} has no lat/lng")
    return float(lat), float(lng)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--fire", type=int, help="geo_event id")
    g.add_argument("--latlng", help="LAT,LNG (e.g. 33.92,-117.24)")
    parser.add_argument("--online-only", action="store_true",
                        help="hide offline feeds")
    args = parser.parse_args()

    client = WatchDutyClient()
    try:
        lat, lng = _resolve_latlng(args, client)
        feeds = client.list_radio_feeds(lat, lng) or []
    except WatchDutyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.online_only:
        feeds = [f for f in feeds if f.get("online")]

    feeds.sort(key=lambda f: (not f.get("online"),
                              -int(f.get("listeners") or 0)))

    if not feeds:
        print(f"no feeds at ({lat:.4f}, {lng:.4f})", file=sys.stderr)
        return 0

    print(f"{len(feeds)} feed(s) near ({lat:.4f}, {lng:.4f}):")
    for f in feeds:
        pill = "ON " if f.get("online") else "off"
        listeners = int(f.get("listeners") or 0)
        name = (f.get("name") or "(unnamed)")[:40]
        url = f.get("listen_url") or ""
        print(f"  [{pill}] {f.get('feed_id', '?'):>6}  {name:<40}  "
              f"{listeners:>4} listeners  {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
