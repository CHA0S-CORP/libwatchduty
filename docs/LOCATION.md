# Auto-locate (`--near auto`)

Most `watchduty` subcommands take `--near LAT,LNG` so they can sort
fires by distance, filter by radius, lookup scanner feeds, etc. Typing
your coordinates every invocation is tedious, so the CLI accepts
`--near auto` and the TUI re-uses the same plumbing via the `:near
auto` command.

This page documents what that does — including the privacy implications
— so you can decide whether to use it or pass coordinates explicitly.

---

## How it resolves

The public entry point is
[`libwatchduty.location.detect_location()`](../src/libwatchduty/location.py).
It tries each source in order until one succeeds, then returns
`(lat, lng, source_label)`:

| Order | Source | Label | When attempted |
|---|---|---|---|
| 1 | macOS CoreLocationCLI shell-out | `corelocation` | macOS *and* `CoreLocationCLI` on `$PATH` |
| 2 | `https://ipapi.co/json/` IP geolocation | `ip:ipapi.co` | always (unless step 1 already returned) |
| 3 | `https://ipwho.is/` IP geolocation | `ip:ipwho.is` | fallback if `ipapi.co` failed |

The whole chain shares a 2-second wall-clock budget by default. Each
step gets at most `_CORELOC_CAP` (1.2 s) or `_HTTP_CAP` (1.5 s)
clamped to the remaining budget; once <50 ms remain we stop trying.
Any error — connection refused, parse error, permission denied,
timeout — is swallowed and the next source runs. The function never
raises; it returns `None` if every source failed inside the budget.

The TUI shows the resolved source in the status bar (e.g.
`⌖ near 33.92,-117.24 ≤250km (corelocation)`) so you can see at a
glance which path produced your coordinates.

---

## Native macOS path (CoreLocationCLI)

If you'd rather not send your IP to a third-party host, install Apple's
Location Services shell wrapper:

```bash
brew install corelocationcli
```

Subsequent `watchduty … --near auto` invocations on macOS will shell
out to `CoreLocationCLI` instead of hitting the network. The **first**
call triggers a one-time consent prompt at
**System Settings → Privacy & Security → Location Services** for the
terminal app that spawned the process (Terminal.app, iTerm, Ghostty,
…). Approve once and later runs are silent.

We invoke it with a short timeout and parse only the
`lat,lng` it prints to stdout. Nothing is persisted; nothing else is
sent anywhere.

---

## IP-geolocation fallbacks

When CoreLocationCLI isn't available (or you're not on macOS), the
chain hits two third-party services in order:

1. `https://ipapi.co/json/`
2. `https://ipwho.is/`

Both work the same way: your public IP arrives at the service in the
TCP source, the service maps `IP → city → approximate lat/lng` from
its own database, and you get a coarse coordinate back (typically
city-centroid accurate). The User-Agent we send is
`watchduty-cli/auto-locate`; no other identifying header is added.

Resolution is *coarse* — usually city-scale. Good enough to seed the
`--within 250` radius the TUI defaults to, not good enough to put you
exactly where you live. If you're behind a corporate proxy or VPN
you'll see the proxy's location, not yours.

---

## Bypassing auto-locate

Pass explicit coordinates:

```bash
watchduty tui --near 33.9276,-117.13208 --within 100
watchduty fires --near 37.77,-122.42 --within 50
```

…or set an environment variable so you don't have to repeat yourself:

```bash
export WATCHDUTY_HOME=37.77,-122.42
watchduty tui                 # uses WATCHDUTY_HOME automatically
```

You can also flip back and forth at runtime from the TUI's `:` prompt:

```
:near 34.0,-117.5             # explicit
:near auto                    # re-run detect_location()
:near off                     # drop the filter; show all fires
```

---

## Programmatic access

```python
from libwatchduty.location import detect_location

got = detect_location(timeout=3.0)
if got is None:
    print("no location available — caller must provide --near")
else:
    lat, lng, source = got
    print(f"detected {lat:.3f},{lng:.3f} via {source}")
```

`detect_location` is the only public symbol. The internal `_try_*`
helpers are deliberately private — the source list and ordering are
subject to change.

---

## Privacy summary

| Mode | What leaves your machine | Avoid by |
|---|---|---|
| `--near LAT,LNG` | Nothing extra | n/a (default if you provide it) |
| `--near auto` on macOS w/ CoreLocationCLI | Nothing over the network; macOS-managed location prompt | n/a |
| `--near auto` w/o CoreLocationCLI | Public IP → ipapi.co (then ipwho.is on failure) | install `corelocationcli` or pass coords explicitly |
| `WATCHDUTY_HOME=auto` | Same as `--near auto` | set to coordinates instead |
