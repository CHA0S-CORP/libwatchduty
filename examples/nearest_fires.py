"""List the closest active wildfires to a coordinate.

Demonstrates:
  - Constructing a `WatchDutyClient` with no auth (read endpoints are public).
  - Filtering server-side by event type, then client-side by distance.
  - Sorting by haversine distance to a fixed point.
  - Pretty printing with right-aligned numerics and containment glyphs.

Run:
    python examples/nearest_fires.py 33.92 -117.24
    python examples/nearest_fires.py 33.92 -117.24 --radius 100 --limit 10

Output (truncated):

    8 active wildfires within 250 km of (33.92, -117.24):
       12.4 km  ▲ #105316  Junction Fire           120 ac  10% ▰▰▱
       45.6 km  ▲ #105110  Cedar Hollow Fire       980 ac  45% ▰▱▱
      128.0 km  ●  #104994 Pine Ridge Fire       3,400 ac  78% ▱▱▱

The threat glyph follows the project's tier mapping: ▰▰▰ red (high),
▰▰▱ amber (medium), ▰▱▱ green (low / contained).
"""

from __future__ import annotations

import argparse
import math
from typing import Iterable

from libwatchduty import WatchDutyClient


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two ``(lat, lng)`` points."""
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


def threat_glyphs(cont: float | None, acres: float) -> str:
    """Cheap surrogate for the TUI's threat bar — three-segment ▰▱ string.

    `tui._threat_factors` does the real composite scoring; this is just
    a glyph that captures "uncontained × size" for the CLI list.
    """
    uncontained = 1.0 if cont is None else max(0.0, 1.0 - cont / 100.0)
    size_norm = min(1.0, math.log10(1 + acres) / math.log10(1 + 1000))
    score = uncontained * size_norm
    if score > 0.6:
        return "▰▰▰"   # high
    if score > 0.25:
        return "▰▰▱"   # medium
    if score > 0.05:
        return "▰▱▱"   # low
    return "▱▱▱"


def _format_rows(
    fires: Iterable[dict], home: tuple[float, float], radius_km: float, limit: int,
) -> list[str]:
    rows: list[tuple[float, dict]] = []
    for f in fires:
        lat, lng = f.get("lat"), f.get("lng")
        if not (isinstance(lat, (int, float)) and isinstance(lng, (int, float))):
            continue
        d = haversine_km(home, (float(lat), float(lng)))
        if d <= radius_km:
            rows.append((d, f))
    rows.sort(key=lambda r: r[0])

    out: list[str] = []
    for dist, f in rows[:limit]:
        data = f.get("data") or {}
        acres = float(data.get("acreage") or 0)
        cont = data.get("containment")
        name = (f.get("name") or "?")[:30]
        cont_s = f"{int(cont)}%" if isinstance(cont, (int, float)) else "  -"
        glyphs = threat_glyphs(cont, acres)
        out.append(
            f"  {dist:>6.1f} km  #{f['id']:<7} {name:<30}  "
            f"{int(acres):>7} ac  {cont_s:>4} {glyphs}"
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("lat", type=float, help="home latitude")
    parser.add_argument("lng", type=float, help="home longitude")
    parser.add_argument("--radius", type=float, default=250.0,
                        help="max distance in km (default 250)")
    parser.add_argument("--limit", type=int, default=20,
                        help="max rows to print (default 20)")
    args = parser.parse_args()

    client = WatchDutyClient()
    fires = client.list_geo_events(types=["wildfire"], active_only=True)
    rows = _format_rows(fires, (args.lat, args.lng), args.radius, args.limit)
    print(f"{len(rows)} active wildfires within "
          f"{args.radius:g} km of ({args.lat}, {args.lng}):")
    for row in rows:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
