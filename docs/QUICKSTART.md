# Quick start

Get a threat-ranked dashboard of every active wildfire near you in about
60 seconds.

## 1 · install

```bash
pip install libwatchduty[tui]
```

- The base install gives you the `WatchDutyClient` Python class and the
  `watchduty` CLI.
- The `[tui]` extra adds [`pyte`](https://pypi.org/project/pyte/), which
  the terminal UI uses to embed mapscii inside the Map tab. The TUI runs
  without it (mapscii fullscreens on `m` instead), but the inline map is
  worth the extra ~5 kB.

> If you want the bundled mapscii Node binary too, run
> `watchduty-install-mapscii` once after install. It checks for
> `node`+`npm` and runs `npm install -g mapscii` for you.

## 2 · launch the dashboard

```bash
watchduty tui --near auto --within 250 --refresh 60
```

- `--near auto` — IP-geolocates your home point (or pass
  `--near LAT,LNG`).
- `--within 250` — clamp the fire list to a 250 km radius.
- `--refresh 60` — re-poll every 60 s (min 30 s; be polite to the API).

You should see, top-to-bottom:
- a dark status strip with `◉ watchduty · filters · ⌖ near …`
- the **fire list** (threat-ranked, 2-row cards with bearing arrows + a
  ▰ threat bar + containment bar)
- a **detail pane** with title banner, KV block (threat breakdown,
  wind, sparklines, distance/acreage trends), a live camera frame, and
  four tabs: **Updates · Radio · Map · Evac**
- a footer with focus chip + key hints

## 3 · move around

| key | action |
|---|---|
| `j / k`           | navigate fire list (LIST focus) or scroll updates (DETAIL focus) |
| `J / K`           | always scroll updates feed |
| `PgUp / PgDn`     | page-scroll updates |
| `Tab`             | toggle focus between list and detail |
| `Enter` / `l`     | open the selected fire's detail pane |
| `1 2 3 4`         | jump to tab: Updates · Radio · Map · Evac |
| `R c e u`         | aliases for Radio / Map / Evac / Updates |
| `←` / `→`         | cycle tabs |
| `z`               | toggle compact one-line list mode |
| `+` / `-`         | zoom mapscii (on Map tab) · cycle inline image size (elsewhere) |
| `m`               | fullscreen mapscii on the selected fire |
| `i`               | fullscreen the live camera frame |
| `P`               | toggle inline camera thumbnail |
| `/`               | filter prompt · `n / N` jump matches |
| `X` or `Ctrl-L`   | clear the active filter |
| `:`               | command prompt (see below) |
| `[` / `]`         | shrink / grow `--within` by 50 km |
| `r`               | refresh fires + selected fire's updates |
| `L`               | toggle LIVE polling (every 30 s) |
| `t / T`           | cycle sort / reverse direction |
| `?`               | help overlay · `q / Ctrl-C` quit |

## 4 · `:` commands

Inside the `:` prompt:

| command | effect |
|---|---|
| `:within N`               | set max distance from `--near` (km) |
| `:near LAT,LNG / auto / off` | re-anchor the home point |
| `:types t1,t2,…`          | filter geo-event types (`wildfire,flooding,hazard,location`) |
| `:sort threat / distance / acreage / updated` | set list sort |
| `:reverse`                | flip current sort direction |
| `:refresh N`              | auto-refresh seconds (≥30; 0 = manual) |
| `:mouse-invert`           | flip wheel-up/down (handy on macOS natural scrolling) |
| `:mouse-debug`            | print raw `bstate` on every mouse event in the footer |

## 5 · scripting against the API

```python
from libwatchduty import WatchDutyClient

c = WatchDutyClient()
for ev in c.list_geo_events(types=["wildfire"], active_only=True):
    print(ev["id"], ev["name"], ev["data"].get("acreage"), "ac")

for r in c.iter_reports(105316):                # one fire's updates
    print(r["date_created"], r["message"][:80])

bundle = c.get_fire_bundle(105316)              # event + reports + radio + cams + fps
```

The client uses `requests.Session` underneath — pass `session=…` to
inject your own (handy for testing with `responses`).

## 6 · what next

- Mark a real saved place: `:near 33.92,-117.24` then `:within 100`.
- Drop into the Map tab — mapscii loads centred on your selected fire
  at zoom 13 with a red ▲ marker. Arrow keys pan, `+ -` zoom.
- Open the Updates tab on a noisy incident, press `L` to enable LIVE
  polling — new updates flash a red `NEW` chip and the terminal bell
  fires + OSC9 toast on iTerm/ghostty.
- Hit `?` any time for the full key map.

## Troubleshooting

- **No fires shown** — `--near` didn't resolve. Try `:near LAT,LNG`
  with explicit coords, or widen `:within 500`.
- **Mouse scroll backwards / silent** — `:mouse-invert` flips it.
  `:mouse-debug` shows what `bstate` your terminal sends.
- **mapscii says "world view"** — make sure the bundled mapscii is
  present (`pip install` ships it as wheel shared-data); the `m` toast
  echoes the lat/lng being passed so you can verify.
- **"terminal too small"** — TUI needs at least 40 cols × 10 rows.

For the full development + release flow see
[`docs/CHAOS_CORP_RELEASE.md`](CHAOS_CORP_RELEASE.md).
