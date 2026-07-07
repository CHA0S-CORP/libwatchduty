"""Small CLI for libwatchduty.

Examples:
    watchduty fires
    watchduty fires --type wildfire,flooding --active
    watchduty fires --near 37.77,-122.41 --within 50 --unit mi
    watchduty fires --near auto
    watchduty event 104994
    watchduty reports 104994
    watchduty bundle 104994 -o fire.json
    watchduty tui --near auto --refresh 60

Subcommands
-----------
fires
    List geo events as a colorized table, with an optional nested update tree.
event
    Show one geo event as a labelled key/value block.
reports
    List approved reports for a geo event as a table.
places, me
    Show user-scoped data (requires ``--token``).
radio
    Broadcastify scanner feeds near a point or fire, grouped online/offline.
cameras
    Wildfire-detection cameras near a point or fire.
fires-modified
    Geo events modified since a given ISO-8601 date/datetime.
bundle
    All data for one fire (event + reports + radio + cameras). ``-o`` writes JSON.
login
    Exchange username/password for an API token.
tui
    Interactive curses dashboard (POSIX terminals only).

Every default (non-JSON) output goes through :mod:`libwatchduty.tables` for
consistent alignment and color. Each list-style subcommand still accepts
``--json``/``--raw`` to emit unformatted ``json.dump`` output for piping.

Environment variables
---------------------
WATCHDUTY_TOKEN
    DRF token used to authenticate API calls. Required for ``places`` and
    ``me``; optional for read-only endpoints.
WATCHDUTY_HOME
    Default value for ``fires --near`` and ``tui --near``. Accepts either a
    literal ``LAT,LNG`` pair or the string ``auto`` to trigger best-effort
    auto-detection via :mod:`libwatchduty.location`.
NO_COLOR
    If set (any value), disables ANSI color output. Honored by
    :mod:`libwatchduty.colors`.
FORCE_COLOR
    If set to anything other than ``"0"``, forces ANSI color output even when
    stdout is not a TTY. ``FORCE_COLOR=0`` explicitly disables it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any

from . import colors as C
from . import tables as T
from ._geo import haversine_km
from ._text import format_age_relative, strip_html
from .client import GEO_EVENT_TYPES, WatchDutyClient, WatchDutyError
from .location import detect_location
from .tui import run as run_tui


def _nonneg_int(s: str) -> int:
    """argparse type: int >= 0."""
    try:
        v = int(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected integer, got {s!r}") from e
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {v}")
    return v


def _positive_int(s: str) -> int:
    """argparse type: int >= 1."""
    try:
        v = int(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected integer, got {s!r}") from e
    if v < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {v}")
    return v


def _nonneg_float(s: str) -> float:
    """argparse type: float >= 0."""
    try:
        v = float(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected number, got {s!r}") from e
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {v}")
    return v


def _positive_float(s: str) -> float:
    """argparse type: float > 0."""
    try:
        v = float(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected number, got {s!r}") from e
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {v}")
    return v


def main(argv: list[str] | None = None) -> int:
    """Run the watchduty CLI.

    Parses ``argv`` (defaults to :data:`sys.argv` ``[1:]``), builds a
    :class:`WatchDutyClient`, dispatches to the chosen subcommand
    (``fires``, ``event``, ``reports``, ``places``, ``me``, ``radio``,
    ``cameras``, ``bundle``, ``login``, ``tui``), and returns a shell exit
    code: ``0`` on success, ``1`` on :class:`WatchDutyError` or unexpected
    failure (with a hint about ``--debug``), ``130`` on Ctrl-C.

    Honors the env vars listed in the module docstring; never raises.
    """
    p = argparse.ArgumentParser(prog="watchduty")
    p.add_argument(
        "--token",
        default=os.environ.get("WATCHDUTY_TOKEN"),
        help="auth token (env: WATCHDUTY_TOKEN; visible to `ps` if passed here)",
    )
    p.add_argument(
        "--token-stdin", action="store_true",
        help="read token from stdin (one line); overrides --token",
    )
    p.add_argument("--debug", action="store_true", help="show full tracebacks on error")
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="extra diagnostics to stderr",
    )
    # Default subcommand is `tui` — bare `watchduty` launches the dashboard.
    sub = p.add_subparsers(dest="cmd", required=False)

    # Shared --raw/--json pair, inherited by every list-style subcommand.
    raw_json = argparse.ArgumentParser(add_help=False)
    raw_json.add_argument("--raw", action="store_true", help="print full JSON")
    raw_json.add_argument("--json", action="store_true", help="alias for --raw")

    fires = sub.add_parser(
        "fires",
        help="list geo events with recent updates",
        description=(
            "List geo events (wildfires by default), optionally filtered to a radius "
            "around a point. Example: "
            "watchduty fires --near 37.77,-122.41 --within 50 --unit mi"
        ),
        parents=[raw_json],
    )
    fires.add_argument(
        "--type", default=",".join(GEO_EVENT_TYPES),
        help="comma-separated geo event types",
    )
    fires.add_argument("--active", action="store_true", help="active fires only")
    fires.add_argument(
        "--updates", type=_nonneg_int, default=1, metavar="N",
        help="recent updates to show per fire (default 1, 0 to skip)",
    )
    fires.add_argument(
        "--limit", type=_nonneg_int, default=0,
        help="cap number of fires (0 = all)",
    )
    fires.add_argument(
        "--workers", type=_positive_int, default=8,
        help="parallel report fetches (>= 1)",
    )
    fires.add_argument(
        "--near", metavar="LAT,LNG|auto",
        default=os.environ.get("WATCHDUTY_HOME"),
        help="filter to fires near this point, or 'auto' to detect "
             "(env: WATCHDUTY_HOME; also accepts 'auto')",
    )
    fires.add_argument(
        "--within", type=_positive_float, default=100.0, metavar="DIST",
        help="max distance from --near in --unit (default 100)",
    )
    fires.add_argument(
        "--unit", choices=("km", "mi"), default="km",
        help="distance unit for --within and display",
    )

    ev = sub.add_parser(
        "event", help="get one geo event as a labelled block", parents=[raw_json],
    )
    ev.add_argument("id", type=int)

    rep = sub.add_parser(
        "reports", help="approved reports for a geo event", parents=[raw_json],
    )
    rep.add_argument("id", type=int)

    sub.add_parser(
        "places", help="user saved places (requires --token)", parents=[raw_json],
    )
    sub.add_parser(
        "me", help="current user (requires --token)", parents=[raw_json],
    )

    rad = sub.add_parser(
        "radio", help="broadcastify scanner feeds near a point or fire",
        parents=[raw_json],
    )
    grad = rad.add_mutually_exclusive_group(required=True)
    grad.add_argument("--latlng", help="lat,lng pair")
    grad.add_argument("--fire", type=int, help="geo_event id")

    cam = sub.add_parser(
        "cameras",
        help="wildfire-detection cameras near a point or fire",
        description="Omit --latlng, --fire and --bbox to list all cameras.",
        parents=[raw_json],
    )
    gcam = cam.add_mutually_exclusive_group()
    gcam.add_argument("--latlng", help="lat,lng pair")
    gcam.add_argument("--fire", type=int, help="geo_event id")
    gcam.add_argument(
        "--bbox", metavar="MIN_LAT,MAX_LAT,MIN_LNG,MAX_LNG",
        help="bounding box; filters client-side",
    )
    cam.add_argument(
        "--limit", type=_nonneg_int, default=50,
        help="max cameras to print (0 = all, default 50)",
    )

    fmod = sub.add_parser(
        "fires-modified",
        help="geo events modified since a given date/datetime (YYYY-MM-DD or ISO-8601)",
        parents=[raw_json],
    )
    fmod.add_argument(
        "since", metavar="MODIFIED_SINCE",
        help="cutoff (YYYY-MM-DD or full ISO-8601 like 2026-06-25T00:00:00Z)",
    )
    fmod.add_argument(
        "--type", default=",".join(GEO_EVENT_TYPES),
        help="comma-separated geo event types",
    )

    bun = sub.add_parser("bundle", help="all data for one fire (event+reports+radio+cams)")
    bun.add_argument("id", type=int)
    bun.add_argument(
        "-o", "--output", metavar="FILE",
        help="write bundle JSON to FILE instead of stdout",
    )

    login = sub.add_parser("login", help="login and print token")
    login.add_argument("username")
    login.add_argument(
        "password", nargs="?", default=None,
        help='password; pass "-" or omit to read interactively via getpass',
    )

    tui_p = sub.add_parser(
        "tui",
        help="interactive curses dashboard (POSIX terminals only)",
        description=(
            "Launch the interactive Watch Duty TUI. With no flags, runs with "
            "--near auto --within 250 --refresh 60 so it Just Works."
        ),
    )
    tui_p.add_argument(
        "--near", metavar="LAT,LNG|auto",
        default=os.environ.get("WATCHDUTY_HOME", "auto"),
        help="filter to fires near this point, or 'auto' to detect "
             "(env: WATCHDUTY_HOME; default: auto)",
    )
    tui_p.add_argument(
        "--type", default=",".join(GEO_EVENT_TYPES),
        help="comma-separated geo event types",
    )
    tui_p.add_argument(
        "--refresh", type=_nonneg_int, default=60, metavar="SECONDS",
        help="auto-refresh interval in seconds (default 60; minimum 30; 0 = manual)",
    )
    tui_p.add_argument(
        "--within", type=_positive_float, default=250.0, metavar="DIST",
        help="max distance from --near in km (default 250)",
    )

    stills_p = sub.add_parser(
        "stills",
        help="download camera still frames (single, batch, or timelapse)",
        description=(
            "Capture wildfire-detection camera stills. Use `list` to enumerate "
            "cameras, `get` for a single frame, `capture` for a one-shot batch, "
            "or `watch` to run a recurring timelapse loop."
        ),
    )
    stills_sub = stills_p.add_subparsers(dest="stills_cmd", required=True)

    stills_sub.add_parser("list", help="list cameras (id, name, latlng, image url)")

    sget = stills_sub.add_parser("get", help="download one still by camera id")
    sget.add_argument("camera_id", help="camera id (e.g. 2732) or composite id")
    sget.add_argument("-o", "--output", metavar="FILE", help="output path; default: ./<auto>.jpg")
    sget.add_argument("--no-show", action="store_true",
                      help="skip inline image render even on kitty terminals")

    sshow = stills_sub.add_parser("show",
                                  help="render a still inline (kitty terminals only)")
    sshow.add_argument("camera_id")
    sshow.add_argument("--rows", type=_nonneg_int, default=20,
                       help="max terminal rows for the image (default 20)")

    scap = stills_sub.add_parser("capture", help="one-shot batch capture")
    scap.add_argument("--out", required=True, metavar="DIR", help="output directory")
    scap.add_argument("--camera", action="append", default=[], metavar="ID",
                      help="repeatable; capture these camera ids")
    scap.add_argument("--near", metavar="LAT,LNG", help="only cameras within --radius km of this point")
    scap.add_argument("--radius", type=_nonneg_float, default=50.0, metavar="KM",
                      help="radius for --near (default 50)")
    scap.add_argument("--limit", type=_nonneg_int, default=0, metavar="N",
                      help="cap number of cameras (0 = all matching)")

    swatch = stills_sub.add_parser("watch", help="recurring timelapse capture loop")
    swatch.add_argument("--out", required=True, metavar="DIR")
    swatch.add_argument("--interval", type=_nonneg_float, default=60.0, metavar="SEC",
                        help="seconds between rounds (default 60)")
    swatch.add_argument("--duration", type=_nonneg_float, default=None, metavar="SEC",
                        help="total run time; omit to loop forever")
    swatch.add_argument("--camera", action="append", default=[], metavar="ID")
    swatch.add_argument("--near", metavar="LAT,LNG")
    swatch.add_argument("--radius", type=_nonneg_float, default=50.0, metavar="KM")
    swatch.add_argument("--limit", type=_nonneg_int, default=0, metavar="N")

    air_p = sub.add_parser(
        "aircraft",
        help="aircraft catalog (global, not fire-scoped)",
    )
    air_sub = air_p.add_subparsers(dest="air_cmd", required=True)
    air_list = air_sub.add_parser("list", help="search the catalog")
    air_list.add_argument("--search", metavar="Q", default="",
                          help="substring match across tail_num/hex/callsign/name/model/type")
    air_list.add_argument("--limit", type=_nonneg_int, default=50)
    air_list.add_argument("--json", action="store_true")
    air_list.add_argument("--refresh", action="store_true", help="bypass on-disk cache")
    air_show = air_sub.add_parser("show", help="single aircraft by tail/hex/callsign")
    air_show.add_argument("query")

    args = p.parse_args(argv)

    # No subcommand → run the TUI with its default flags (the most useful
    # entry point for a bare `watchduty` invocation).
    if args.cmd is None:
        args.cmd = "tui"
        args.near = args.near if hasattr(args, "near") else os.environ.get("WATCHDUTY_HOME", "auto")
        args.type = ",".join(GEO_EVENT_TYPES)
        args.refresh = 60
        args.within = 250.0

    if args.token_stdin:
        line = sys.stdin.readline().strip()
        if line:
            args.token = line
    elif args.token and any(
        a == "--token" or a.startswith("--token=")
        for a in (argv if argv is not None else sys.argv[1:])
    ):
        # WHY: tokens on argv leak via `ps` listings and shell history.
        print(
            "note: --token on command line is visible to `ps`; prefer "
            "WATCHDUTY_TOKEN env var or --token-stdin",
            file=sys.stderr,
        )

    c = WatchDutyClient(token=args.token)

    try:
        if args.cmd == "fires":
            return _cmd_fires(c, args)
        if args.cmd == "event":
            return _cmd_event(c, args)
        if args.cmd == "reports":
            return _cmd_reports(c, args)
        if args.cmd == "places":
            if not args.token:
                p.error("--token or WATCHDUTY_TOKEN required for `places`")
            return _cmd_places(c, args)
        if args.cmd == "me":
            if not args.token:
                p.error("--token or WATCHDUTY_TOKEN required for `me`")
            return _cmd_me(c, args)
        if args.cmd == "radio":
            return _cmd_radio(c, args)
        if args.cmd == "cameras":
            return _cmd_cameras(c, args)
        if args.cmd == "fires-modified":
            return _cmd_fires_modified(c, args)
        if args.cmd == "bundle":
            return _cmd_bundle(c, args)
        if args.cmd == "login":
            return _cmd_login(c, args)
        if args.cmd == "tui":
            return _cmd_tui(args)
        if args.cmd == "stills":
            return _cmd_stills(c, args)
        if args.cmd == "aircraft":
            return _cmd_aircraft(c, args)
    except WatchDutyError as e:
        print(
            C.paint(f"error: {e}", C.ERROR, stream=sys.stderr),
            file=sys.stderr,
        )
        if e.body:
            try:
                print(json.dumps(e.body, indent=2)[:1000], file=sys.stderr)
            except (TypeError, ValueError):
                print(str(e.body)[:1000], file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # WHY: downstream pager/pipe closed early; suppress noisy traceback.
        try:
            sys.stderr.close()
        except Exception:
            pass
        return 0
    except Exception as e:  # noqa: BLE001
        if args.debug:
            raise
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        print("  (run with --debug for traceback)", file=sys.stderr)
        return 1
    return 0


# ----------------------------------------------------------------------------
# Color callables for table cells
# ----------------------------------------------------------------------------


def _color_fire_id(_v: Any, _row: Any) -> str:
    return C.FIRE_ID


def _color_active_mark(_v: Any, row: Any) -> str:
    return C.FIRE_NAME_ACTIVE if (isinstance(row, dict) and row.get("is_active")) else C.DIM_ROLE


def _color_type_tag(_v: Any, _row: Any) -> str:
    return C.TYPE_TAG


def _color_fire_name(_v: Any, row: Any) -> str:
    return (
        C.FIRE_NAME_ACTIVE
        if (isinstance(row, dict) and row.get("is_active"))
        else C.FIRE_NAME_INACTIVE
    )


def _color_distance(_v: Any, _row: Any) -> str:
    return C.DISTANCE


def _color_size_cont(_v: Any, row: Any) -> str:
    # Mixed cell; pick whichever color is more informative. Containment wins
    # when present (it carries good/bad signal), else acreage color.
    d = (row.get("data") or {}) if isinstance(row, dict) else {}
    cont = d.get("containment")
    if cont is not None:
        try:
            return C.CONTAINMENT_GOOD if float(cont) >= 50 else C.CONTAINMENT_BAD
        except (TypeError, ValueError):
            return C.CONTAINMENT_BAD
    return C.ACREAGE


def _color_address(_v: Any, _row: Any) -> str:
    return C.ADDRESS


def _color_heading(_v: Any, _row: Any) -> str:
    return C.HEADING


def _color_update_text(_v: Any, _row: Any) -> str:
    return C.UPDATE_TEXT


def _color_timestamp(_v: Any, _row: Any) -> str:
    return C.TIMESTAMP


def _color_reporter(_v: Any, _row: Any) -> str:
    return C.REPORTER


def _color_status_approved(v: Any, _row: Any) -> str:
    return C.OK if str(v).lower() == "approved" else C.ERROR


def _color_online(_v: Any, row: Any) -> str:
    return C.OK if (isinstance(row, dict) and row.get("__online__")) else C.ERROR


def _color_acreage(_v: Any, _row: Any) -> str:
    return C.ACREAGE


def _color_cam_state(v: Any, _row: Any) -> str:
    return C.ERROR if str(v).strip().upper() == "OFFLINE" else C.OK


def _color_ptz(v: Any, _row: Any) -> str:
    return C.TYPE_TAG if str(v).strip() else C.DIM_ROLE


# ----------------------------------------------------------------------------
# Subcommand implementations
# ----------------------------------------------------------------------------


def _cmd_fires(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `fires` subcommand."""
    types = [t.strip() for t in args.type.split(",") if t.strip()]
    evs = c.list_geo_events(types=types, active_only=args.active)
    home = _resolve_home(args.near)
    distances: dict = {}
    skipped_no_coords = 0
    if home:
        limit_km = args.within if args.unit == "km" else args.within * 1.609344
        annotated = []
        for e in evs:
            if e.get("lat") is None or e.get("lng") is None:
                skipped_no_coords += 1
                continue
            dkm = haversine_km(home, (e["lat"], e["lng"]))
            if dkm <= limit_km:
                annotated.append((dkm, e))
        annotated.sort(key=lambda x: x[0])
        evs = [e for _, e in annotated]
        distances = {e.get("id"): d for d, e in annotated if e.get("id") is not None}
    cleaned = []
    dropped_no_id = 0
    for e in evs:
        if e.get("id") is None:
            dropped_no_id += 1
            continue
        cleaned.append(e)
    evs = cleaned
    if args.limit:
        evs = evs[: args.limit]
    if args.raw or args.json:
        json.dump(evs, sys.stdout, indent=2)
        print()
        if dropped_no_id:
            print(f"note: skipped {dropped_no_id} events without ids", file=sys.stderr)
        if skipped_no_coords:
            print(
                f"note: skipped {skipped_no_coords} events without coordinates",
                file=sys.stderr,
            )
        return 0

    updates = (
        _fetch_updates(c, evs, args.updates, args.workers, verbose=args.verbose)
        if args.updates > 0
        else {}
    )
    _print_fires_table(evs, updates, args.updates, distances, args.unit)
    if dropped_no_id:
        print(f"note: skipped {dropped_no_id} events without ids", file=sys.stderr)
    if skipped_no_coords:
        print(
            f"note: skipped {skipped_no_coords} events without coordinates",
            file=sys.stderr,
        )
    return 0


def _cmd_event(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `event` subcommand."""
    data = c.get_geo_event(args.id) or {}
    if args.raw or args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        if not data:
            print("note: empty response", file=sys.stderr)
        return 0
    if not data:
        # Render a single error row but still return 0 (caller asked for one
        # event by id; emptiness is informational not fatal).
        rows = [{"label": "note", "value": "empty response"}]
        cols = [
            T.Column("Field", "label", align="right", width=18, color=lambda *_: C.ERROR),
            T.Column("Value", "value", align="left", width=80, color=lambda *_: C.ERROR),
        ]
        print(T.render_table(rows, cols))
        print("note: empty response", file=sys.stderr)
        return 0
    items = _event_kv_items(data)
    print(T.render_kv(items))
    # Then render a separator + flat table of data sub-dict for completeness.
    sub = data.get("data") or {}
    if isinstance(sub, dict) and sub:
        print()
        rows = [{"k": k, "v": _scalar_or_json(v, 200)} for k, v in sub.items()]
        cols = [
            T.Column("Field", "k", align="right", width=20, color=_color_heading),
            T.Column(
                "Value", "v", align="left", width=80,
                color=_color_update_text, truncate=True,
            ),
        ]
        print(T.render_table(rows, cols))
    return 0


def _cmd_reports(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `reports` subcommand."""
    data = c.get_reports(args.id) or []
    if args.raw or args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        if not data:
            print("note: no reports for this event", file=sys.stderr)
        return 0
    if not data:
        # Render empty table headers anyway for visual symmetry.
        print(T.render_table([], _reports_columns()))
        print("note: no reports for this event", file=sys.stderr)
        return 0
    print(T.render_table(data, _reports_columns()))
    return 0


def _reports_columns() -> list[T.Column]:
    def _when(row):
        s = row.get("date_created") or ""
        abs_ = s[:16].replace("T", " ")
        rel = format_age_relative(s)
        return f"{abs_} · {rel}" if rel else abs_

    def _reporter(row):
        return (row.get("user_created") or {}).get("display_name") or "?"

    def _latlng(row):
        lat = row.get("lat")
        lng = row.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            return f"{lat:.4f},{lng:.4f}"
        return "-"

    def _msg(row):
        s = strip_html(row.get("message") or "", para_sep=" ¶ ")
        return s if len(s) <= 120 else s[:117] + "..."

    return [
        T.Column("ID", "id", align="right", width=7, color=_color_fire_id, truncate=False),
        T.Column("When", _when, align="left", width=16, color=_color_timestamp, truncate=False),
        T.Column("Reporter", _reporter, align="left", width=20, color=_color_reporter),
        T.Column(
            "Status", "status", align="left", width=9,
            color=_color_status_approved, truncate=False,
        ),
        T.Column("Lat,Lng", _latlng, align="right", width=18, color=_color_distance, truncate=False),
        T.Column("Message", _msg, align="left", width=80, color=_color_update_text),
    ]


def _cmd_places(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `places` subcommand."""
    data = c.get_places() or []
    if args.raw or args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        if not data:
            print("note: no saved places", file=sys.stderr)
        return 0
    if not data:
        print("note: no saved places", file=sys.stderr)
        return 0
    # Two layouts: a single-place response is rendered as kv; a list as a table.
    if isinstance(data, list):
        print(T.render_table(data, _places_columns()))
    elif isinstance(data, dict):
        print(T.render_kv(_flatten_kv(data)))
    return 0


def _places_columns() -> list[T.Column]:
    def _type(row):
        return row.get("type") or row.get("kind") or ""

    def _latlng(row):
        lat = row.get("lat")
        lng = row.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            return f"{lat:.4f},{lng:.4f}"
        return "-"

    def _radius(row):
        r = row.get("radius_km")
        if r is not None:
            return f"{r} km"
        r = row.get("radius")
        if r is not None:
            return str(r)
        return "-"

    def _notify(row):
        on = row.get("notification_enabled")
        if on is None:
            on = row.get("push_enabled")
        if on is None:
            return ""
        return "on" if bool(on) else "off"

    def _color_notify(v, _row):
        s = str(v).strip().lower()
        if s == "on":
            return C.OK
        if s == "off":
            return C.DIM_ROLE
        return C.DIM_ROLE

    return [
        T.Column("ID", "id", align="right", width=6, color=_color_fire_id, truncate=False),
        T.Column("Name", "name", align="left", width=24, color=_color_heading),
        T.Column("Type", _type, align="left", width=12, color=_color_type_tag),
        T.Column("Lat,Lng", _latlng, align="right", width=18, color=_color_distance, truncate=False),
        T.Column("Radius", _radius, align="right", width=9, color=_color_acreage, truncate=False),
        T.Column("Notify", _notify, align="center", width=7, color=_color_notify, truncate=False),
        T.Column("Address", "address", align="left", width=40, color=_color_address),
    ]


def _cmd_me(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `me` subcommand."""
    data = c.get_user() or {}
    if args.raw or args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        return 0
    items = _flatten_kv(data)
    print(T.render_kv(items))
    return 0


def _cmd_radio(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `radio` subcommand."""
    lat, lng = _resolve_latlng(c, args)
    feeds = c.list_radio_feeds(lat, lng) or []
    county = c.get_radio_county(lat, lng) or {}
    if args.raw or args.json:
        json.dump({"county": county, "feeds": feeds}, sys.stdout, indent=2)
        print()
        return 0

    name = county.get("name") or "?"
    url = county.get("url") or ""
    print(C.paint(f"County: {name} -> {url}", C.BOLD))

    # Group: online first, offline second. Inject a synthetic __online__ key
    # on each row so the color callable + sort key share the same source.
    rows = []
    for f in feeds:
        row = dict(f)
        row["__online__"] = bool(f.get("online"))
        if args.verbose and (f.get("feed_id") is None or f.get("listen_url") is None):
            print(f"  warn: feed missing fields: {f!r}", file=sys.stderr)
        rows.append(row)
    rows.sort(key=lambda r: (0 if r["__online__"] else 1, -(r.get("listeners") or 0)))

    def _status(row):
        return "ON" if row.get("__online__") else "off"

    def _listen(row):
        url_ = row.get("listen_url") or ""
        if not url_:
            return ""
        return C.hyperlink(url_, url_)

    cols = [
        T.Column("Status", _status, align="center", width=6, color=_color_online, truncate=False),
        T.Column("Feed ID", "feed_id", align="right", width=7, color=_color_fire_id, truncate=False),
        T.Column("Name", "name", align="left", width=40, color=_color_heading),
        T.Column("Description", "description", align="left", width=40, color=_color_update_text),
        T.Column(
            "Listeners", "listeners", align="right", width=9,
            color=_color_acreage, truncate=False,
        ),
        T.Column("Listen URL", _listen, align="left", width=50, color=_color_address),
    ]
    print(T.render_table(rows, cols))
    return 0


def _cmd_cameras(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `cameras` subcommand."""
    if getattr(args, "bbox", None):
        parts = [p.strip() for p in args.bbox.split(",")]
        if len(parts) != 4:
            raise SystemExit(
                "error: --bbox expects MIN_LAT,MAX_LAT,MIN_LNG,MAX_LNG"
            )
        try:
            min_lat, max_lat, min_lng, max_lng = (float(p) for p in parts)
        except ValueError:
            raise SystemExit(
                f"error: --bbox values must be numeric (got {args.bbox!r})"
            ) from None
        cams = c.list_cameras_in_bbox(min_lat, max_lat, min_lng, max_lng)
    elif args.latlng or args.fire:
        lat, lng = _resolve_latlng(c, args)
        cams = c.list_cameras(lat, lng)
    else:
        cams = c.list_cameras()
    cams = cams or []
    if args.raw or args.json:
        json.dump(cams, sys.stdout, indent=2)
        print()
        return 0

    total = len(cams)
    shown = cams if args.limit == 0 else cams[: args.limit]
    # Group live first, offline last.
    shown_sorted = sorted(shown, key=lambda x: 1 if x.get("is_offline") else 0)
    print(T.render_table(shown_sorted, _cameras_columns()))
    if args.limit and total > args.limit:
        print(
            "  " + C.paint(f"... +{total - args.limit} more", C.DIM_ROLE)
        )
    return 0


def _cameras_columns() -> list[T.Column]:
    def _latlng(row):
        ll = row.get("latlng") or {}
        lat = ll.get("lat")
        lng = ll.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            return f"{lat:.4f},{lng:.4f}"
        return "-"

    def _ptz(row):
        return "PTZ" if row.get("has_ptz") else ""

    def _state(row):
        return "OFFLINE" if row.get("is_offline") else "live"

    def _img(row):
        u = row.get("image_url") or ""
        if not u:
            return ""
        return C.hyperlink(u, u)

    return [
        T.Column("ID", "id", align="right", width=8, color=_color_fire_id, truncate=False),
        T.Column("Name", "name", align="left", width=35, color=_color_heading),
        T.Column("Lat,Lng", _latlng, align="right", width=18, color=_color_distance, truncate=False),
        T.Column("PTZ", _ptz, align="center", width=4, color=_color_ptz, truncate=False),
        T.Column("State", _state, align="center", width=8, color=_color_cam_state, truncate=False),
        T.Column("Image URL", _img, align="left", width=60, color=_color_address),
    ]


def _cmd_fires_modified(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `fires-modified` subcommand."""
    types = [t.strip() for t in args.type.split(",") if t.strip()]
    data = c.list_geo_events_modified_since(args.since, types=types)
    if args.raw or args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        if not data:
            print("note: no events modified since cutoff", file=sys.stderr)
        return 0

    def _modified(row):
        s = row.get("date_modified") or ""
        abs_ = s[:16].replace("T", " ")
        rel = format_age_relative(s)
        return f"{abs_} · {rel}" if rel else abs_

    cols = [
        T.Column("ID", "id", align="right", width=7, color=_color_fire_id, truncate=False),
        T.Column("Type", "geo_event_type", align="left", width=10, color=_color_type_tag),
        T.Column("Name", "name", align="left", width=36, color=_color_fire_name),
        T.Column("Modified", _modified, align="left", width=26, color=_color_timestamp, truncate=False),
        T.Column("Address", "address", align="left", width=40, color=_color_address),
    ]
    print(T.render_table(data or [], cols))
    if not data:
        print("note: no events modified since cutoff", file=sys.stderr)
    return 0


def _cmd_bundle(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `bundle` subcommand."""
    bundle = c.get_fire_bundle(args.id)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2, default=str)
            fh.write("\n")
        print(f"wrote bundle to {args.output}", file=sys.stderr)
        return 0
    # Stdout: overview kv-header, then JSON dump (preserve old behavior for
    # scripts piping bundle JSON), per spec.
    _print_bundle_summary(bundle)
    print()
    try:
        json.dump(bundle, sys.stdout, indent=2, default=str)
        print()
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            sys.stderr.close()
        except Exception:
            pass
    return 0


def _print_bundle_summary(bundle: dict) -> None:
    """Render the bundle's per-section overview table above the JSON dump."""
    event = bundle.get("event") or {}
    reports = bundle.get("reports") or []
    fps_runs = bundle.get("fps_runs") or []
    radio_feeds = bundle.get("radio_feeds")
    radio_county = bundle.get("radio_county")
    cameras = bundle.get("cameras")

    def _evt_summary() -> str:
        name = event.get("name") or "(unnamed)"
        typ = event.get("geo_event_type") or "?"
        act = "ACTIVE" if event.get("is_active") else "inactive"
        return f"{name} | {typ} | {act}"

    def _reports_summary() -> str:
        if not reports:
            return "-"
        latest = (reports[0].get("date_created") or "")[:16].replace("T", " ")
        return f"latest: {latest}"

    def _county_summary() -> str:
        if not isinstance(radio_county, dict):
            return "-"
        return radio_county.get("name") or "?"

    sections = [
        ("event", 1, _evt_summary()),
        ("reports", len(reports), _reports_summary()),
        ("fps_runs", len(fps_runs), _scalar_or_json(fps_runs, 200)),
    ]
    if radio_feeds is not None:
        sections.append(("radio_feeds", len(radio_feeds), f"{len(radio_feeds)} feeds"))
    else:
        sections.append(("radio_feeds", "-", "(skipped: no coords)"))
    if radio_county is not None:
        sections.append(("radio_county", 1, _county_summary()))
    else:
        sections.append(("radio_county", "-", "(skipped: no coords)"))
    if cameras is not None:
        sections.append(("cameras", len(cameras), f"{len(cameras)} cameras"))
    else:
        sections.append(("cameras", "-", "(skipped: no coords)"))

    def _summary_color(_v: Any, row: Any) -> str:
        if isinstance(row, dict) and str(row.get("count")) == "-":
            return C.DIM_ROLE
        return C.UPDATE_TEXT

    rows = [{"section": s, "count": ct, "summary": sm} for s, ct, sm in sections]
    cols = [
        T.Column("Section", "section", align="left", width=14, color=_color_heading, truncate=False),
        T.Column("Count", "count", align="right", width=6, color=_color_acreage, truncate=False),
        T.Column("Summary", "summary", align="left", width=80, color=_summary_color),
    ]
    print(T.render_table(rows, cols))


def _cmd_login(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Handle the `login` subcommand."""
    password = args.password
    if password is None or password == "-":
        password = getpass.getpass("Password: ")
    r = c.login(args.username, password) or {}
    # Same key set the client accepts in login().
    tok = r.get("key") or r.get("token") or r.get("auth_token")
    if not tok:
        # Preserve sentinel error output for callers grepping stderr.
        rows = [{"label": "error", "value": "login response has no key/token"}]
        cols = [
            T.Column("Field", "label", align="right", width=12, color=lambda *_: C.ERROR),
            T.Column("Value", "value", align="left", width=80, color=lambda *_: C.ERROR),
        ]
        print(T.render_table(rows, cols))
        print("error: login response has no key/token", file=sys.stderr)
        try:
            print(json.dumps(r), file=sys.stderr)
        except (TypeError, ValueError):
            print(str(r), file=sys.stderr)
        return 1
    user = r.get("user") or {}
    items: list[tuple[str, Any]] = [
        ("token", C.paint(tok, C.OK)),
        ("username", user.get("username")),
        ("user_id", user.get("id")),
        ("email", user.get("email")),
    ]
    if r.get("expires"):
        items.append(("expires", r.get("expires")))
    print(T.render_kv(items))
    # Stable marker line for legacy scripts that grep "TOKEN:".
    print(f"TOKEN: {tok}")
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    """Handle the `tui` subcommand."""
    near, source = _resolve_home_verbose(args.near) if args.near else (None, "")
    types = [t.strip() for t in args.type.split(",") if t.strip()]
    return run_tui(
        token=args.token,
        near=near,
        types=types or None,
        auto_refresh=args.refresh,
        within_km=args.within,
        near_source=source,
    )


def _resolve_home_verbose(near: str | None) -> tuple[tuple[float, float] | None, str]:
    """Like ``_resolve_home`` but also returns a short source label.

    Returns ``((lat, lng), source)`` on success, ``(None, "")`` on failure
    or when ``near`` is falsy.
    """
    if not near:
        return None, ""
    if near.strip().lower() == "auto":
        detected = detect_location()
        if detected is None:
            print(
                C.paint("warning: auto-locate failed; running unfiltered",
                        C.DIM_ROLE, stream=sys.stderr),
                file=sys.stderr,
            )
            return None, ""
        lat, lng, source = detected
        print(
            C.paint(f"detected location: {lat:.2f},{lng:.2f} ({source})",
                    C.DIM_ROLE, stream=sys.stderr),
            file=sys.stderr,
        )
        return (lat, lng), source
    try:
        lat, lng = _parse_latlng(near)
        return (lat, lng), "manual"
    except SystemExit:
        return None, ""


def _cmd_stills(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Dispatch `stills {list,get,capture,watch}`."""
    from . import stills as S

    sub = args.stills_cmd
    if sub == "list":
        cams = c.list_cameras()
        cols = [
            T.Column("ID", "id", "right", 8, True, None),
            T.Column("Name", lambda r: (r.get("name") or "")[:40], "left", 42, True, None),
            T.Column("Lat,Lng", lambda r: _fmt_latlng(r.get("latlng") or {}), "left", 18, False, None),
            T.Column("State", lambda r: "OFFLINE" if r.get("is_offline") else "live", "left", 8, False, None),
            T.Column("Image URL", lambda r: r.get("image_url") or "", "left", 0, False, None),
        ]
        sys.stdout.write(T.render_table(cams, cols) + "\n")
        return 0

    if sub == "get":
        cams = c.list_cameras()
        cid = args.camera_id
        match = next((x for x in cams if str(x.get("id")) == str(cid)), None)
        if match is None:
            try:
                match = c.get_camera_gis(cid)
            except WatchDutyError as e:
                print(f"error: camera {cid} not found: {e}", file=sys.stderr)
                return 1
        url = match.get("image_url")
        if not url:
            print("error: camera has no image_url", file=sys.stderr)
            return 1
        out = args.output or S.still_filename(match)
        n = S.save_still(c, url, out)
        print(f"wrote {n} bytes -> {out}", file=sys.stderr)
        if not getattr(args, "no_show", False):
            from . import images as I
            if I.supports_inline_images():
                with open(out, "rb") as fh:
                    data = fh.read()
                sys.stdout.write(I.render_inline(data, max_rows=24))
                sys.stdout.write("\n")
                sys.stdout.flush()
        return 0

    if sub == "show":
        from . import images as I
        cams = c.list_cameras()
        cid = args.camera_id
        match = next((x for x in cams if str(x.get("id")) == str(cid)), None)
        if match is None:
            try:
                match = c.get_camera_gis(cid)
            except WatchDutyError as e:
                print(f"error: camera {cid} not found: {e}", file=sys.stderr)
                return 1
        url = match.get("image_url")
        if not url:
            print("error: camera has no image_url", file=sys.stderr)
            return 1
        if not I.supports_inline_images():
            print(
                C.paint(
                    "note: stills show requires kitty/ghostty/iTerm2; "
                    "falling back to URL",
                    C.DIM_ROLE, stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            print(C.hyperlink(url, url))
            return 0
        try:
            data = c.fetch_camera_image(url)
        except WatchDutyError as e:
            print(f"error: fetch failed: {e}", file=sys.stderr)
            return 1
        sys.stdout.write(I.render_inline(data, max_rows=args.rows))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0

    if sub == "capture":
        near = _parse_latlng(args.near) if args.near else (None, None)
        written = S.capture_cameras(
            c,
            out_dir=args.out,
            camera_ids=args.camera or None,
            lat=near[0], lng=near[1],
            radius_km=args.radius if args.near else None,
            limit=args.limit or None,
        )
        print(f"wrote {len(written)} stills under {args.out}", file=sys.stderr)
        for p in written:
            print(p)
        return 0 if written else 1

    if sub == "watch":
        near = _parse_latlng(args.near) if args.near else (None, None)
        kwargs = dict(
            camera_ids=args.camera or None,
            lat=near[0], lng=near[1],
            radius_km=args.radius if args.near else None,
            limit=args.limit or None,
        )
        try:
            for batch in S.capture_loop(
                c,
                out_dir=args.out,
                interval=args.interval,
                duration=args.duration,
                **kwargs,
            ):
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
                print(f"{ts}  wrote {len(batch)} stills", file=sys.stderr)
        except KeyboardInterrupt:
            print("interrupted", file=sys.stderr)
            return 130
        return 0

    return 1


def _cmd_aircraft(c: WatchDutyClient, args: argparse.Namespace) -> int:
    """Dispatch `aircraft {list,show}` against the cached catalog."""
    from . import aircraft as A

    sub = args.air_cmd
    catalog = A.load_catalog(c, force_refresh=getattr(args, "refresh", False))
    if sub == "list":
        # --limit 0 means "all", matching the cameras subcommand.
        limit = args.limit or len(catalog)
        rows = A.lookup(catalog, args.search, limit=limit) if args.search else catalog[:limit]
        if args.json:
            json.dump(rows, sys.stdout, indent=2)
            print()
            return 0
        cols = [
            T.Column("Tail", lambda r: r.get("tail_num") or "", "left", 10, True, None),
            T.Column("Hex", lambda r: r.get("hex_code") or "", "left", 8, True, None),
            T.Column("Callsign", lambda r: r.get("short_callsign") or "", "left", 12, True, None),
            T.Column("Type", lambda r: r.get("type") or "", "left", 16, True, None),
            T.Column("Model", lambda r: r.get("model") or "", "left", 18, True, None),
            # width=0 + truncate=True renders an empty column; natural
            # width needs truncate=False.
            T.Column("Name", lambda r: r.get("name") or "", "left", 0, False, None),
        ]
        sys.stdout.write(T.render_table(rows, cols) + "\n")
        print(f"({len(rows)} match{'es' if len(rows)!=1 else ''}"
              f"{' from catalog of ' + str(len(catalog)) if args.search else ''})",
              file=sys.stderr)
        return 0
    if sub == "show":
        hits = A.lookup(catalog, args.query, limit=1)
        if not hits:
            print(f"no aircraft matches {args.query!r}", file=sys.stderr)
            return 1
        items = [(k, str(v) if v is not None else "") for k, v in hits[0].items()]
        sys.stdout.write(T.render_kv(items) + "\n")
        return 0
    return 1


def _fmt_latlng(ll: dict) -> str:
    """Render latlng dict as 'lat,lng' or '-' when missing."""
    lat, lng = ll.get("lat"), ll.get("lng")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return f"{lat:.4f},{lng:.4f}"
    return "-"


# ----------------------------------------------------------------------------
# Helpers for kv rendering
# ----------------------------------------------------------------------------


def _scalar_or_json(v: Any, width: int) -> str:
    """Format a scalar as str(), or a nested object as truncated JSON."""
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        s = str(v)
    else:
        try:
            s = json.dumps(v, default=str)
        except (TypeError, ValueError):
            s = str(v)
    if width and len(s) > width:
        s = s[: max(1, width - 1)] + "…"
    return s


def _flatten_kv(d: dict, prefix: str = "", width: int = 200) -> list[tuple[str, Any]]:
    """Flatten a nested dict into (dotted-key, scalar-or-json) tuples."""
    out: list[tuple[str, Any]] = []
    if not isinstance(d, dict):
        out.append((prefix or "value", _scalar_or_json(d, width)))
        return out
    for k, v in d.items():
        label = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            # Recurse one level; deeper levels collapse to JSON.
            if any(isinstance(vv, (dict, list)) for vv in v.values()):
                out.append((label, _scalar_or_json(v, width)))
            else:
                out.extend(_flatten_kv(v, label, width))
        elif isinstance(v, list):
            out.append((label, _scalar_or_json(v, width)))
        else:
            out.append((label, _scalar_or_json(v, width)))
    return out


def _event_kv_items(data: dict) -> list[tuple[str, Any]]:
    """Return a stable ordered list of label/value pairs for `event`."""
    items: list[tuple[str, Any]] = []
    eid = data.get("id")
    if eid is not None:
        link = f"https://app.watchduty.org/i/{eid}"
        items.append(("id", C.hyperlink(C.paint(str(eid), C.FIRE_ID), link)))
        items.append(("page", C.paint(link, C.LINK)))
    name = data.get("name")
    if name is not None:
        items.append(
            ("name", C.paint(str(name), C.FIRE_NAME_ACTIVE if data.get("is_active") else C.FIRE_NAME_INACTIVE))
        )
    if data.get("geo_event_type") is not None:
        items.append(("type", C.paint(str(data["geo_event_type"]), C.TYPE_TAG)))
    if data.get("is_active") is not None:
        a = bool(data["is_active"])
        items.append(("active", C.paint("yes" if a else "no", C.OK if a else C.DIM_ROLE)))
    lat, lng = data.get("lat"), data.get("lng")
    if lat is not None and lng is not None:
        items.append(("lat,lng", C.paint(f"{lat},{lng}", C.DISTANCE)))
    if data.get("address"):
        items.append(("address", C.paint(str(data["address"]), C.ADDRESS)))
    sub = data.get("data") or {}
    if isinstance(sub, dict):
        if sub.get("acreage") is not None:
            items.append(("data.acreage", C.paint(f"{sub['acreage']}", C.ACREAGE)))
        if sub.get("containment") is not None:
            try:
                pct = float(sub["containment"])
            except (TypeError, ValueError):
                pct = 0.0
            items.append(("data.containment", T.render_bar(pct)))
        for src_key, label in (
            ("evacuation_orders", "evac_orders"),
            ("evacuation_warnings", "evac_warnings"),
            ("evacuation_advisories", "evac_advisories"),
        ):
            v = sub.get(src_key)
            if v:
                items.append((label, C.paint(strip_html(v, para_sep=" ¶ "), C.EVAC_ORDER if "orders" in src_key else C.EVAC_WARN)))
        links = sub.get("links")
        if isinstance(links, list) and links:
            link_lines = []
            for ln in links[:5]:
                if isinstance(ln, dict) and ln.get("value"):
                    label = ln.get("label") or ln.get("link_type") or "link"
                    link_lines.append(C.hyperlink(label, ln["value"]))
            if link_lines:
                items.append(("links", " | ".join(link_lines)))
    if data.get("started_at"):
        items.append(("started", C.paint(str(data["started_at"]), C.TIMESTAMP)))
    if data.get("updated_at"):
        items.append(("updated", C.paint(str(data["updated_at"]), C.TIMESTAMP)))
    if data.get("date_created"):
        items.append(("date_created", C.paint(str(data["date_created"]), C.TIMESTAMP)))
    if data.get("date_modified"):
        items.append(("date_modified", C.paint(str(data["date_modified"]), C.TIMESTAMP)))
    evac = data.get("evac_zones")
    if isinstance(evac, list):
        items.append(("evac_zones", f"{len(evac)} zones"))
    elif evac is not None:
        items.append(("evac_zones", _scalar_or_json(evac, 200)))
    return items


# ----------------------------------------------------------------------------
# resolve helpers
# ----------------------------------------------------------------------------


def _resolve_home(near: str | None) -> tuple[float, float] | None:
    """Turn ``--near`` (or ``WATCHDUTY_HOME``) into a ``(lat, lng)`` pair.

    Accepts either a literal ``"lat,lng"`` or the sentinel string ``"auto"``
    (case-insensitive). When ``auto`` is requested, calls
    :func:`libwatchduty.location.detect_location`; on success a one-line dim
    note is emitted to stderr, on failure a dim warning is emitted and the
    function returns ``None`` so the caller can skip the distance filter
    instead of aborting.

    Returns ``None`` when no ``near`` value was supplied or when auto-detect
    fell through.
    """
    if not near:
        return None
    if near.strip().lower() == "auto":
        detected = detect_location()
        if detected is None:
            print(
                C.paint(
                    "warning: auto-locate failed; skipping --near filter",
                    C.DIM_ROLE,
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            return None
        lat, lng, source = detected
        print(
            C.paint(
                f"using detected location: {lat:.1f}, {lng:.1f} ({source})",
                C.DIM_ROLE,
                stream=sys.stderr,
            ),
            file=sys.stderr,
        )
        return lat, lng
    return _parse_latlng(near)


def _resolve_latlng(c, args) -> tuple[float, float]:
    """Resolve a subcommand's ``--latlng`` or ``--fire`` flag to a numeric (lat, lng).

    Prefers ``--latlng`` when present; otherwise fetches the geo event named
    by ``--fire`` and reads its coordinates. Exits via :class:`SystemExit` if
    the chosen fire has no coordinates on file.
    """
    if args.latlng:
        return _parse_latlng(args.latlng)
    ev = c.get_geo_event(args.fire) or {}
    lat, lng = ev.get("lat"), ev.get("lng")
    if lat is None or lng is None:
        raise SystemExit(f"fire {args.fire} has no lat/lng; pass --latlng instead")
    return float(lat), float(lng)


def _parse_latlng(s: str) -> tuple[float, float]:
    """Parse a ``"LAT,LNG"`` string into a validated ``(float, float)`` tuple.

    Latitude must be in ``[-90, 90]`` and longitude in ``[-180, 180]``.
    Malformed input or out-of-range coordinates exit via :class:`SystemExit`
    with a human-readable message — this is a CLI helper, not a library API,
    so a hard exit is preferable to a raw traceback.
    """
    parts = s.split(",")
    if len(parts) != 2:
        raise SystemExit(
            f"error: expected LAT,LNG like 37.77,-122.41 (got {s!r})"
        )
    try:
        lat = float(parts[0])
        lng = float(parts[1])
    except ValueError:
        raise SystemExit(
            f"error: LAT,LNG must be numeric (got {s!r})"
        ) from None
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lng <= 180.0:
        raise SystemExit(
            f"error: LAT,LNG out of range (got {lat},{lng})"
        )
    return lat, lng


def _fetch_updates(
    c: WatchDutyClient,
    events: list[dict],
    n: int,
    workers: int,
    *,
    verbose: bool = False,
) -> dict:
    """Concurrently fetch up to ``n`` recent reports per event.

    Spins up a :class:`ThreadPoolExecutor` of at most ``workers`` threads,
    calls ``client.get_reports`` for each event id, and catches per-event
    :class:`WatchDutyError` so a single bad event never poisons the batch.
    On failure the slot stores the sentinel string ``"__error__:<msg>"``;
    :func:`_print_fires_table` renders that as an ``(error: ...)`` placeholder
    instead of an updates subtree.

    Parameters
    ----------
    c:
        Client used for the report fetches.
    events:
        Event dicts; only ``id`` is consulted.
    n:
        Maximum reports per event (forwarded as ``limit``).
    workers:
        Upper bound on concurrent HTTP requests; clamped to ``>= 1``.
    verbose:
        When True, individual fetch errors are echoed to stderr.

    Returns
    -------
    dict
        Map of ``event_id -> list[dict]`` on success, or
        ``event_id -> "__error__:<msg>"`` on per-event failure.
    """
    from concurrent.futures import ThreadPoolExecutor

    def grab(eid):
        try:
            return eid, c.get_reports(eid, limit=n, offset=0) or []
        except WatchDutyError as e:
            if verbose:
                print(f"  warn: reports({eid}) failed: {e}", file=sys.stderr)
            return eid, f"__error__:{e}"

    out: dict = {}
    eids = [e.get("id") for e in events if e.get("id") is not None]
    if not eids:
        return out
    try:
        from tqdm import tqdm  # type: ignore
        bar = tqdm(total=len(eids), desc="updates", unit="fire",
                   leave=False, disable=not sys.stderr.isatty())
    except Exception:
        bar = None
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for eid, reps in pool.map(grab, eids):
            out[eid] = reps
            if bar is not None:
                bar.update(1)
    if bar is not None:
        bar.close()
    return out


def _print_fires_table(
    events: list[dict],
    updates: dict,
    n_updates: int,
    distances: dict | None = None,
    unit: str = "km",
) -> None:
    """Render fires as a colorized table, with nested update lines per row.

    Uses :func:`tables.render_table` for the parent row and a manual tree-glyph
    render for the children (so the columns stay aligned).
    """
    distances = distances or {}
    factor = 1.0 if unit == "km" else 1 / 1.609344

    def _active_mark(row):
        return "*" if row.get("is_active") else "-"

    def _dist(row):
        eid = row.get("id")
        if eid is not None and eid in distances:
            return f"{distances[eid] * factor:.1f} {unit}"
        return "-"

    def _size_cont(row):
        d = row.get("data") or {}
        acres = d.get("acreage")
        cont = d.get("containment")
        parts: list[str] = []
        if acres:
            parts.append(f"{acres} ac")
        if cont is not None:
            parts.append(f"{cont}% cont")
        return " | ".join(parts) if parts else "-"

    cols = [
        T.Column("ID", "id", align="right", width=7, color=_color_fire_id, truncate=False),
        T.Column("A", _active_mark, align="center", width=1, color=_color_active_mark, truncate=False),
        T.Column("Type", "geo_event_type", align="left", width=10, color=_color_type_tag),
        T.Column("Name", "name", align="left", width=36, color=_color_fire_name),
        T.Column("Dist", _dist, align="right", width=9, color=_color_distance, truncate=False),
        T.Column("Size/Cont", _size_cont, align="right", width=18, color=_color_size_cont),
        T.Column("Address", "address", align="left", width=40, color=_color_address),
    ]

    # Render the table once to get aligned parent rows, then interleave nested
    # update lines under each row using tree glyphs.
    table_text = T.render_table(events, cols)
    lines = table_text.split("\n") if table_text else []

    # The first 2 lines (header + separator) come straight through; the rest
    # correspond 1:1 with events.
    header_lines: list[str] = []
    data_lines: list[str] = []
    if lines:
        # render_table emits header + separator + N data lines.
        header_lines = lines[:2]
        data_lines = lines[2:]

    for line in header_lines:
        print(line)

    for e, parent_line in zip(events, data_lines):
        print(parent_line)
        if n_updates <= 0:
            continue
        eid = e.get("id")
        reps = updates.get(eid) if eid is not None else None
        if isinstance(reps, str) and reps.startswith("__error__:"):
            err_msg = reps[len("__error__:"):]
            print("  " + C.paint(C.TREE_LAST + f"(error: {err_msg})", C.ERROR))
            continue
        reps = reps or []
        if not reps:
            print("  " + C.paint(C.TREE_LAST + "(no updates)", C.DIM_ROLE))
            continue
        shown = reps[:n_updates]
        for i, r in enumerate(shown):
            is_last = i == len(shown) - 1
            glyph = C.TREE_LAST if is_last else C.TREE_BRANCH
            who = (r.get("user_created") or {}).get("display_name") or "?"
            ts = r.get("date_created") or ""
            when = ts[:16].replace("T", " ")
            rel = format_age_relative(ts)
            ts_block = f"[{when}]" + (f" {rel}" if rel else "")
            msg = strip_html(r.get("message") or "", para_sep=" ¶ ")
            if len(msg) > 180:
                msg = msg[:177] + "..."
            print(
                "  "
                + C.paint(glyph, C.BRANCH)
                + C.paint(ts_block, C.TIMESTAMP)
                + " "
                + C.paint(who, C.REPORTER)
                + ": "
                + C.paint(msg, C.UPDATE_TEXT)
            )


if __name__ == "__main__":
    sys.exit(main())
