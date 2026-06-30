# libwatchduty

> Unofficial Watch Duty wildfire dashboard — Python client for `api.watchduty.org` plus a threat-ranked terminal UI.

Reverse-engineered from the [app.watchduty.org](https://app.watchduty.org) browser app. **No affiliation with Watch Duty.** Read endpoints are public; user-scoped endpoints (saved places, profile) require login.

---

<!-- absolute raw.githubusercontent URLs so the screenshots render
     on PyPI's project page, not just on github.com -->
![update view](https://raw.githubusercontent.com/CHA0S-CORP/libwatchduty/main/docs/screenshots/update-view.jpg)
![map view](https://raw.githubusercontent.com/CHA0S-CORP/libwatchduty/main/docs/screenshots/map-view.jpg)
---

## Features

- **Threat scoring** — composite per-fire score combining proximity, size, containment, growth rate, and wind speed/bearing; visible as a 3-segment `▰▱` bar in the list.
- **Live updates polling** — background worker thread re-fetches geo events and reports on a configurable interval (`--refresh`), with toast notifications when new reports arrive.
- **Radio embed** — Broadcastify scanner feeds for the county the selected fire sits in, online feeds grouped first, listener counts inline.
- **Inline cameras** — wildfire-detection camera stills rendered in-terminal on kitty / ghostty / iTerm2 (Kitty graphics protocol), with size and on/off toggles.
- **Embedded mapscii** — the Map tab embeds [mapscii](https://github.com/rastapasta/mapscii) inside the right pane via a pyte-backed PTY; full-screen handoff on `m`.
- **Compact list mode** — toggle between two-line "card" rows and a single-line dense view (`z`).
- **Threat-ranked sort** — default sort by threat when `--near` is set; cycles through threat / distance / acreage / updated (`t`, reverse with `T`).
- **Filter + search** — incremental `/`-filter across name/type, `n`/`N` to jump matches, `X` to clear.
- **Pure-Python deps** — `requests` + `tqdm` for the core; `pyte` only when the inline mapscii embed is wanted.
- **CLI for scripting** — `watchduty fires`, `event`, `reports`, `bundle`, `radio`, `cameras`, `stills`, `aircraft` — every list subcommand has `--json` for piping.
- **Auto-locate** — `--near auto` resolves to your approximate latitude/longitude via IP geolocation; otherwise pass `LAT,LNG`.
- **Camera still capture** — one-shot `stills capture` or a recurring `stills watch` timelapse loop.

---

## Install

```bash
pip install libwatchduty            # CLI + client
pip install libwatchduty[tui]       # inline mapscii embed (pyte)\
```

Python ≥ 3.9. The `tui` extra only adds [`pyte`](https://pypi.org/project/pyte/) for the embedded map — the dashboard itself runs without it (you get full-screen mapscii via `m` instead).

---

## Quick start

> The full walkthrough — install, first launch, keybindings cheat-sheet,
> `:` commands, scripting against the API — lives in
> **[`docs/QUICKSTART.md`](docs/QUICKSTART.md)**.

```bash
pip install libwatchduty[tui]

watchduty tui --near auto --within 250 --refresh 60
watchduty fires --active
watchduty event 105316
watchduty reports 105316
```

Bare `watchduty` (no subcommand) launches the TUI with sensible defaults — `--near auto --within 250 --refresh 60`. Set `WATCHDUTY_HOME=37.77,-122.42` (or `auto`) to skip the flag.

Python client:

```python
from libwatchduty import WatchDutyClient

c = WatchDutyClient()
for ev in c.list_geo_events(types=["wildfire"]):
    if ev["is_active"]:
        print(ev["id"], ev["name"], ev["data"].get("acreage"), "ac")
```

---

## TUI keybindings

Read off the live source in [`src/libwatchduty/tui.py`](src/libwatchduty/tui.py). Focus-aware bindings depend on whether the **list** (left) or **detail** (right) pane is active.

### Navigation

| Key | Action |
|---|---|
| `j` / `↓` | Move selection down (list focus) **or** scroll detail one line (detail focus) |
| `k` / `↑` | Move selection up (list focus) **or** scroll detail up one line (detail focus) |
| `J` | Scroll detail pane down (always, regardless of focus) |
| `K` | Scroll detail pane up (always, regardless of focus) |
| `PgDn` | Scroll detail pane one page down |
| `PgUp` | Scroll detail pane one page up |
| `g g` | Jump to first fire (chord) |
| `G` | Jump to last fire |
| `Ctrl-D` | Half-page down (selection) |
| `Ctrl-U` | Half-page up (selection) |
| `h` | Focus list pane |
| `l` | Focus detail pane (loads reports for selected fire) |
| `←` | Previous detail tab; double-tap returns focus to list |
| `→` | Next detail tab |
| `Tab` | Cycle focus (list ↔ detail) — within detail, cycles tabs |
| `Shift-Tab` | Reverse tab cycle within detail |
| `Enter` | Open selected fire in detail pane |
| `Esc` / `Backspace` | Return focus to list |

### Detail tabs

| Key | Tab |
|---|---|
| `1` / `u` | Updates (reports feed) |
| `2` / `R` | Radio (Broadcastify scanner feeds) |
| `3` / `c` | Map (embedded mapscii) |
| `4` / `e` | Evac (evacuation zones) |

### Map & image controls

| Key | Action |
|---|---|
| `m` | Launch fullscreen mapscii at the selected fire |
| `+` / `=` | Zoom mapscii in (when map active) **or** bump inline-image size |
| `-` / `_` | Zoom mapscii out (when map active) **or** shrink inline-image size |
| `i` / `F` | Open selected fire's header image fullscreen (kitty/ghostty/iTerm2 only) |
| `p` | Toggle inline header camera image |

### Filters, sort, refresh

| Key | Action |
|---|---|
| `/` | Open incremental name/type filter |
| `X` / `Ctrl-L` | Clear filter |
| `n` | Jump to next match |
| `N` | Jump to previous match |
| `:` | Open command prompt |
| `t` | Cycle sort key (threat → distance → acreage → updated) |
| `T` | Reverse sort direction |
| `[` | Shrink `--within` radius by 50 km |
| `]` | Grow `--within` radius by 50 km |
| `r` | Force refresh of fires + reports for selected event |
| `L` | Toggle LIVE mode (faster polling of reports for selected fire) |
| `z` | Toggle compact list (one line per fire vs. two-line card) |
| `?` | Help overlay |
| `q` | Quit |

---

## Threat scoring

> ⚠️ **WIP — v1 is intentionally rough.** The formula below is a
> triage hint, not a fire-behavior model: it's linear in proximity,
> saturates at ~1000 ac, and collapses to zero on stale `containment`
> values. A **v2 model** is in development — opt in with
> `:threat-model v2` (or `WATCHDUTY_THREAT_MODEL=v2`). See
> **[`docs/THREAT_SCORING.md`](docs/THREAT_SCORING.md)** for the v2
> formula, a side-by-side comparison on five example fires, and the
> roadmap.

Each fire gets a composite `[0, 100]` score that drives the default sort and the colored `▰▱` bar. The formula multiplies normalized factors so any near-zero input collapses the score (a 100k-acre fire at 600 km still scores low):

> **score = clamp(100 · proximity · (0.4 + 0.6·size) · uncontained · growth · wind · bearing, 0, 100)**

where `proximity = 1 − dist/within_km` (clamped to `[0,1]`), `size = log10(1+acres) / log10(1+1000)`, `uncontained = 1 − containment/100`, `growth = 1 + 0.5·growth_rate` (acreage delta over the rolling history window), `wind = 1 + 0.04·wind_mph`, and `bearing` scales `[0.5, 1.5]` based on how directly the wind blows from the fire toward you. Prescribed/planned burns are capped at 5. Tiers: `≥60` red, `≥20` amber, `<6` dim, else green.

---

## mapscii setup

The Map tab embeds [mapscii](https://github.com/rastapasta/mapscii) (an ASCII-art world map client). A pinned, working checkout is bundled under [`vendor/mapscii/`](vendor/mapscii/) and ships as wheel data — the TUI prefers `vendor/mapscii/node_modules/.bin/mapscii` over anything in `$PATH`.

If the bundled copy isn't usable (e.g. you installed from sdist without Node, or `node_modules` is missing), drop to the fallback installer:

```bash
watchduty-install-mapscii
```

This pulls and builds mapscii in a user-writable cache directory. Without mapscii at all, the Map tab degrades gracefully — you can still see the fire's lat/lng and jump to fullscreen rendering (`m`).

---

## Architecture

`client.py` owns the typed HTTP surface against `api.watchduty.org` (sessioned `requests`, paginated `iter_*` helpers, optional DRF token auth). The TUI's `run()` spins up a single background **worker thread** that drains a `queue.Queue` of typed request tuples (`REFRESH_FIRES`, `LOAD_REPORTS`, `LOAD_IMAGE`, …) and posts results onto an output queue. The main curses loop owns a `_TuiState` dataclass (fires list, reports cache, threat scores, focus + scroll positions, filter/sort state) — every keypress mutates `_TuiState`, every drained worker result mutates `_TuiState`, and the curses view is a pure render of that state. Result: input stays responsive while API calls run, with no async/asyncio anywhere.

---

## Testing

```bash
pip install .[test,tui]
pytest tests/
```

Tests use `pyte` to render the TUI inside a fake PTY and assert on the resulting frame, plus straight unit tests over `client.py` against canned fixtures.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, lint/test loop, and how to regenerate screenshots. Release notes live in [CHANGELOG.md](CHANGELOG.md).

---

## License

MIT. See [LICENSE](LICENSE) if present; otherwise the `license = { text = "MIT" }` declaration in [`pyproject.toml`](pyproject.toml) is authoritative.
