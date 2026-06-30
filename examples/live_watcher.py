"""Poll one fire's reports feed and print new updates as they arrive.

This is the headless equivalent of the TUI's LIVE mode (`L` key) —
useful when you want the alerts in a terminal log, Slack, ntfy, etc.

Demonstrates:
  - Long-running polling loop that's polite to api.watchduty.org
    (defaults to one hit every 30 s; floor enforced).
  - Diffing the latest `iter_reports()` page against a seen-ids set so
    each report only fires the alert once.
  - Graceful shutdown on Ctrl-C.
  - Optional shell-out to ``ntfy`` / ``notify-send`` / ``terminal-notifier``
    so the alert pops a desktop notification — disabled by default.

Run:
    python examples/live_watcher.py 105316
    python examples/live_watcher.py 105316 --interval 60 --notify

The fire id is the geo_event id you'd see in the TUI title bar.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
import time

from libwatchduty import WatchDutyClient

MIN_POLL_SECONDS = 30   # be polite — match TUI's _LIVE_POLL_SECONDS


def _notify(title: str, body: str) -> None:
    """Best-effort desktop notification. Silent if no tool is on PATH."""
    if shutil.which("terminal-notifier"):
        subprocess.run(
            ["terminal-notifier", "-title", title, "-message", body],
            check=False, capture_output=True,
        )
        return
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", title, body],
            check=False, capture_output=True,
        )
        return
    if shutil.which("ntfy"):
        subprocess.run(
            ["ntfy", "send", "--title", title, body],
            check=False, capture_output=True,
        )
        return
    # Last resort: OSC 9 escape (iTerm2 / ghostty growl-style toast).
    sys.stdout.write(f"\x1b]9;{title}: {body}\x07")
    sys.stdout.flush()


def _format_report(r: dict) -> str:
    ts = (r.get("date_created") or "")[:19].replace("T", " ")
    who = (r.get("user_created") or {}).get("display_name") or "?"
    msg = (r.get("message") or "").replace("\n", " ").strip()
    return f"[{ts}] {who}: {msg[:200]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fire_id", type=int, help="geo_event id to watch")
    parser.add_argument("--interval", type=int, default=MIN_POLL_SECONDS,
                        help=f"poll interval in seconds "
                             f"(min {MIN_POLL_SECONDS}, default 30)")
    parser.add_argument("--notify", action="store_true",
                        help="send desktop notification on new updates")
    args = parser.parse_args()
    interval = max(MIN_POLL_SECONDS, args.interval)

    client = WatchDutyClient()
    seen_ids: set[int] = set()

    # Seed the seen set so we don't alert on backlog.
    initial = list(client.iter_reports(args.fire_id))
    for r in initial:
        rid = r.get("id")
        if isinstance(rid, int):
            seen_ids.add(rid)
    name = initial[0].get("geo_event", {}).get("name") if initial else f"#{args.fire_id}"
    print(f"watching fire {args.fire_id} "
          f"({len(seen_ids)} reports in backlog) — Ctrl-C to stop",
          file=sys.stderr)

    # Clean Ctrl-C without a stack trace.
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    while True:
        try:
            reports = list(client.iter_reports(args.fire_id))
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            print(f"poll error: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(interval)
            continue
        fresh = [r for r in reports
                 if isinstance(r.get("id"), int) and r["id"] not in seen_ids]
        for r in reversed(fresh):  # oldest first
            print(_format_report(r))
            if args.notify:
                _notify(f"Watch Duty — {name}", _format_report(r))
            seen_ids.add(int(r["id"]))
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
