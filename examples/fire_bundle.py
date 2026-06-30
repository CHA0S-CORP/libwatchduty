"""Dump the entire bundle for one fire as pretty JSON.

A "bundle" is the aggregate of every endpoint that's relevant to a
single geo_event:

  - event details (`/geo_events/{id}`)
  - all approved reports (paginated via `iter_reports`)
  - Broadcastify scanner feeds for the fire's lat/lng
  - PTZ cameras near the fire
  - Fire Progression Simulation (FPS) runs

Demonstrates:
  - The convenience `get_fire_bundle(id)` method (one call).
  - JSON-friendly output suitable for `jq`, `fx`, or piping into a
    spreadsheet via `jq -r '.reports[] | [.date_created, .message] | @tsv'`.

Run:
    python examples/fire_bundle.py 105316 > bundle.json
    python examples/fire_bundle.py 105316 | jq .event.name
    python examples/fire_bundle.py 105316 | jq '.cameras[0]'
"""

from __future__ import annotations

import argparse
import json
import sys

from libwatchduty import WatchDutyClient, WatchDutyError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fire_id", type=int, help="geo_event id")
    parser.add_argument("--no-fps", action="store_true",
                        help="skip fire-progression model runs")
    parser.add_argument("--indent", type=int, default=2,
                        help="JSON indent (use 0 for compact)")
    args = parser.parse_args()

    client = WatchDutyClient()
    try:
        bundle = client.get_fire_bundle(args.fire_id)
    except WatchDutyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.no_fps:
        bundle.pop("fps_runs", None)

    json.dump(bundle, sys.stdout,
              indent=args.indent or None, default=str, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
