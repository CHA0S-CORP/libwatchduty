# Handoff: WatchDuty Wildfire TUI — Dashboard Redesign

## Overview
This is a redesign of the `libwatchduty` interactive terminal dashboard (the `tui.py` curses
app). It is a read-only, two-pane situational-awareness view for active wildfires near the
user: a **threat-ranked fire list** on the left and a rich **incident detail pane** on the
right (metadata, live camera, and tabbed Updates / Radio / Map / Evac).

The redesign fixes concrete problems in the current TUI and adds decision-support signal:

- **Text no longer overflows** the right pane (the current build clips update bodies off-screen).
- **Threat-ranked sort** replaces distance-only sort, so the most dangerous incident surfaces
  even when it isn't the closest.
- **Column headers + a legend** decode the previously cryptic `F/L`, `*`, and color codes.
- **Consistent containment bars** on every row (with explicit `n/a` / `PLANNED`).
- **Trends**: acreage and distance sparklines computed from the update/poll history.
- **Spatial context**: per-row bearing arrows + a Map tab plotting nearby fires relative to you.
- **Inline camera frame** and inline update image thumbnails (instead of `1 image` text refs).
- **Tabs** (Updates / Radio / Map / Evac) consolidate the side-pane toggles into one region.
- **Clarified status bar** with labeled tokens and a refresh countdown.

## About the Design Files
The file in this bundle (`Wildfire TUI Improved.dc.html`) is a **design reference created in
HTML** — a prototype showing the intended look, layout, and behavior. It is **not production
code to copy**. It is built as a "Design Component" and renders inside the design tool's runtime
(`support.js`); opened bare in a browser it will not paint. Treat it as a precise visual spec.

Your task is to **recreate this design in the WatchDuty TUI's real environment** (Python, see
recommendations below) using its established patterns — or, if the team decides to build a web
version, to pick the appropriate web stack and implement the same design there.

## Fidelity
**High-fidelity (hifi).** Colors, glyphs, layout ratios, and copy are final. Reproduce them
faithfully. The one thing that is illustrative rather than literal is the **dummy data** (fire
names, acreages, timestamps) — that comes from the live `WatchDutyClient`.

---

## Framework & Library Recommendations

### The decision: migrate from raw `curses` → Textual

The current `tui.py` is hand-rolled `curses`: manual pane geometry, manual scroll math, manual
color-pair management, a custom worker-thread/queue bridge, and bespoke `addnstr` clipping. Every
feature in this redesign (tabs, a real right-side scrollbar, wrapped text, hover/focus states,
chips, trend rows) is **significantly cheaper to build and maintain in a higher-level TUI
framework**. Recommended primary path:

- **[Textual](https://textual.textualize.io/) (Textualize)** — strongly recommended.
  - CSS-like styling (the design's colors/spacing map almost 1:1 to Textual CSS).
  - Built-in widgets that match this design directly: `DataTable` (fire list), `TabbedContent`/`Tabs`
    (Updates/Radio/Map/Evac), `VerticalScroll` + native scrollbars (updates feed), `Static`/`Label`
    with Rich markup (detail KV block).
  - Reactive attributes + `@work` async workers replace the manual `queue.Queue` + thread plumbing
    (keep `WatchDutyClient` as-is; call it from Textual workers).
  - Mouse, focus, key bindings, resize, and a real event loop are handled for you.
  - Runs over SSH and in the same terminals curses targets; POSIX + Windows.
- **[Rich](https://rich.readthedocs.io/)** — the rendering layer under Textual; use directly for:
  - `rich.text.Text` styled spans (reporter name, timestamps, chips).
  - `rich.bar.Bar` / a custom 1/8th-block renderer for the containment bars (the current
    `_draw_bar` 1/8th-block logic ports over cleanly).
  - Markup strings for the colored KV values.

If the team wants to **stay on stdlib `curses`** (the module docstring prizes stdlib-only): it is
possible but you will be re-implementing tabs, scrollbars, and text wrapping by hand. If you go
this route, keep the existing architecture and add: a tab-state enum in `_TuiState`, a wrapped-text
helper (use `textwrap.wrap(width=detail_w-2)` — this is the actual overflow fix), and a scrollbar
column drawn from `detail_scroll / len(lines)`. Not recommended over Textual unless the
zero-dependency constraint is hard.

- **urwid** — viable older alternative to Textual; weaker styling story, skip unless already in use.

### Supporting libraries

- **Sparklines** (acreage / distance trends): trivial to roll by hand with the block ramp
  `▁▂▃▄▅▆▇█` (map each value to `ramp[round(v_norm*7)]`). If you want a library:
  [`sparklines`](https://pypi.org/project/sparklines/). For richer in-terminal charts,
  [`plotext`](https://github.com/piccolomo/plotext) draws line/scatter plots in the cell grid.
- **Bearing / distance math**: no new dep — you already have `_haversine_km`. Add an
  `_initial_bearing(a, b)` using `atan2` (formula in the Map section) and bucket to 8-point
  compass for the arrow glyphs.
- **Aircraft chips**: keep the existing `aircraft.py` extract/enrich pipeline; render hits as
  Rich-styled chips.
- **Images** (camera frame, update thumbnails): keep the existing `images.py` kitty path; see the
  Map section for the broader protocol matrix.
- **Async HTTP** (optional): if you adopt Textual workers you can keep synchronous `requests`
  inside a thread worker, or migrate to `httpx` for native async. Not required.

### If a web version is chosen instead
- **Stack**: React + TypeScript + Vite, or SvelteKit. The two-pane layout is plain CSS grid/flex.
- **Terminal aesthetic**: keep JetBrains Mono and the exact token palette below; the design is
  already valid HTML/CSS you can lift structurally.
- **Live updates**: SSE or polling against the same endpoints.

---

## Map Recommendations

The design has **two** map surfaces with different jobs. Treat them separately.

### 1. Per-row bearing arrow + KV "bearing" line (always visible)
Pure math, no rendering lib. For user point `U=(lat,lng)` and fire `F=(lat,lng)`:

```
θ = atan2( sin(Δlng)·cos(F.lat),
           cos(U.lat)·sin(F.lat) − sin(U.lat)·cos(F.lat)·cos(Δlng) )
bearing_deg = (degrees(θ) + 360) mod 360
```

Bucket `bearing_deg` into 8 compass points → glyph: `N ↑ · NE ↗ · E → · SE ↘ · S ↓ · SW ↙ · W ← · NW ↖`.
Distance is the existing `_haversine_km`. This is the cheapest, highest-value spatial cue — do it first.

### 2. The "Map" tab — plotting nearby fires relative to you
Pick based on how much fidelity you want and which terminals you must support. Listed cheapest → richest:

- **A. Quadrant/radar plot (what the mock shows) — recommended default.**
  Project each fire to screen XY by bearing + clamped distance onto concentric range rings
  (e.g. 125 km / 250 km). Marker = colored glyph (`◆` selected, `▲/●/○` others); user `◎` at
  center; N/S/E/W labels. No dependency, works in *every* terminal, reads instantly. The mock's
  positions are illustrative — compute real ones from bearing/distance.
- **B. Braille sub-cell canvas — higher resolution, still text.**
  [`drawille`](https://github.com/asciimoo/drawille) (or Textual's canvas) packs 2×4 dots per cell
  via Unicode Braille, so you can draw smoother fire scatter, range rings, and even rough
  fire-perimeter polylines (`data.geometry` if available) at ~4× resolution. Still no GPU/image
  protocol needed.
- **C. ASCII coastline / county basemap.**
  If you want geographic grounding, render a low-res static basemap (state/county outline for the
  query bbox) as background characters, then overlay markers from B. Precompute outlines from a
  simplified GeoJSON (e.g. US counties via `shapely`/`geojson` simplified offline) — do **not**
  fetch tiles per frame (the module is explicitly "be polite to api.watchduty.org"; same spirit
  for third-party tile servers).
- **D. True raster map inline (richest, terminal-gated).**
  Render an actual map image in-cell on capable terminals. You already gate the camera image on
  `images.supports_kitty`; reuse that detection and fall back to A/B when unsupported.
  - **kitty graphics protocol** — already wired in `images.py`; best quality where available.
  - **sixel** — broad-ish support (xterm, mlterm, foot, WezTerm); lib: `libsixel` /
    [`pysixel`](https://pypi.org/project/PySixel/) or Textual's image add-ons.
  - **iTerm2 inline images** — macOS iTerm2 only.
  - Source image: render your own from cached vector data, or a single static map you have rights
    to use. Keep it read-only and rate-limited to match the app's politeness contract.

**Recommended path:** ship **A** (quadrant plot) now — it satisfies the design, has zero deps, and
degrades to nothing. Add **B (drawille)** when you want perimeters/scatter resolution. Reserve **D**
for terminals you already detect for the camera frame.

### If web version
- **[MapLibre GL JS](https://maplibre.org/)** (open, no token) or **Leaflet** + OpenStreetMap
  tiles. Plot fires as markers colored by threat; draw `data.geometry` perimeters as GeoJSON
  layers; center/zoom to the `within_km` bbox around the user.

---

## Screen: Wildfire Dashboard (single full-terminal view)

Fixed reference canvas in the mock: **1600 × 900**. In a real terminal it is fluid; the mock's
pixel sizes encode *ratios*, not literal cell counts.

### Region map
```
┌───────────────────────────────────────────────────────────────────────────┐
│ STATUS BAR (1 row)                                              [● LIVE]    │
├──────────────────────────────┬────────────────────────────────────────────┤
│ FIRE LIST  (~42% width)      │  DETAIL PANE  (~58% width)                  │
│  • column header row         │   • title: #id (sub) + Name (large)         │
│  • 9 fire rows               │   • link                                    │
│  • legend row (bottom)       │   • KV metadata block  |  live camera frame │
│                              │   • tab bar: Updates / Radio / Map / Evac   │
│                              │   • active tab panel (scrolls)              │
├──────────────────────────────┴────────────────────────────────────────────┤
│ (list legend)               │  KEYBIND FOOTER (1 row)                       │
└───────────────────────────────────────────────────────────────────────────┘
```

### Status bar (top, 1 row)
Background `#11151b`, bottom border `#1c2128`, text `#9aa4b0`, labels dim `#6b7681`, padding 6px 12px,
items separated by generous gaps. Tokens, left→right:
- `◉ watchduty` — green `#3fb950`, bold (app identity).
- `filters wildfire,location,flooding,hazard` — label dim, value `#c9d1d9`.
- `⌖ near 33.92,−117.24 · ≤250km · ip:ipapi.co` — coords `#c9d1d9`, source `ip:ipapi.co` cyan `#58c5ff`.
- `sort ▼ THREAT` — value amber `#e3b341` bold (active sort key; cycles distance/acreage/updated/threat).
- `9 of 354` — visible/total count (`len(visible_fires)` / `len(fires)`).
- `refresh ▓▓▓░ 48s` — a small progress meter + countdown to next auto-refresh.
- Far right: `● LIVE` pill — black `#06120a` on green `#3fb950`, bold, when live-poll mode is on.

### Fire list (left, ~42%)
- **Column header row**: bg `#0f1318`, bottom border `#1c2128`, text `#6b7681`, 12px, letter-spacing .5px.
  Columns: `THREAT · DIR · DIST · SIZE · CONTAINMENT · INCIDENT`.
- **Row grid** (monospace), tracks: `THREAT 78px · DIR 20px · DIST 60px(right) · SIZE 58px(right) ·
  CONTAINMENT 132px · INCIDENT 1fr`, `gap 6px`, padding `3px 12px`, rows sorted by **threat desc**.
  - **THREAT cell**: a 3-segment bar `▰▰▰ / ▰▰▱ / ▰▱▱ / ▱▱▱` colored by tier + numeric score.
    Tiers: red `#ff6a5f` (high ≥ ~60), amber `#e3b341` (med ~20–60), green `#3fb950` (low), dim
    `#6b7681` (planned/score≈5). Score is bold in the tier color.
  - **DIR cell**: bearing arrow toward the fire, nav color `#6fb3d6` (dim `#6b7681` for planned rows).
  - **DIST**: right-aligned, `#e8eef4` (active) / `#9aa4b0` (planned), e.g. `8.0km`, `95.3km`.
  - **SIZE**: right-aligned acreage `0ac`/`635ac`, or `—` when unknown; dim when 0/unknown.
  - **CONTAINMENT**: a 10-cell bar. Filled `█` in green `#3fb950` + `░`/`·` track `#283039`, with a
    trailing `%`. When unknown: dim dotted `··········` + `n/a`. For planned burns: literal `PLANNED`.
  - **INCIDENT**: fire name. A leading red `▲` marks a **growing** incident. Selected row name is
    `#fff` bold; active uncontained names red `#ff6a5f`; contained `#9aa4b0`; planned dim `#6b7681`.
  - **Selected row**: bg `#16314c`, inset left accent bar `box-shadow: inset 3px 0 0 #58c5ff`.
  - **Planned/prescribed rows**: whole row `opacity: .62`.
- **Legend row** (bottom): bg `#0f1318`, top border, 12px dim. Reads:
  `▰▰▰ threat = proximity × size × (1−containment) × growth` · `▲ growing` · `DIR: arrow toward fire`.
  (Update the legend text to match the final formula — see Threat Scoring.)

### Detail pane (right, ~58%), padding 8px 16px
- **Vertical separator**: 1px `#1c2128` between list and detail.
- **Title block**:
  - `#105316` — **subheader**, dim `#6b7681`, 12px, letter-spacing .5px.
  - `Junction Fire ↗` — **large**, red `#ff6a5f`, bold, 23px, line-height 1.15; the `↗` is cyan
    `#58c5ff` 16px (opens the incident in the browser).
  - URL line: `https://app.watchduty.org/i/{id}` cyan `#58c5ff`, underlined, 13px.
- **Top row = KV block (left, max ~480px) + live camera (right, fills remaining)**, flex, gap 24px,
  align-items flex-start.
  - **KV block**: 2-col grid `104px 1fr`, row-gap 2px, col-gap 8px, 13.5px, labels dim `#6b7681`.
    Rows in order:
    - **threat** — score `78` red bold 16px + `▰▰▰`; second line (12px, `#8a929c`):
      `proximity 0.62 × size 0.30 × uncontained 1.00 × growth 3.4× × wind 1.4× × bearing 0.9×`
      (numbers `#c9d1d9`, the escalating factors red `#ff6a5f`).
    - **address** — `25000 Block of CA-79, Santa Ysabel`.
    - **coords** — `33.1873, −116.7009`.
    - **bearing** — `↘ SE` (cyan) `· 95.3 km from you`.
    - **wind** — `↗ 14 mph WSW, gusts 24` (amber `#e3b341`) `· driving spread toward you (CA-79 corridor)`.
    - **distance** — flex row, gap 14px: value `95.3 km` bold `#e8eef4` · descending sparkline
      `▇▆▅▃` amber (letter-spacing 3px) · `▼ −1.8km / 27m` red bold · `closing (97.1→95.3)` dim.
    - **acreage** — flex row, gap 14px: `11 ac` amber bold · ascending sparkline `▁▂▃▇` amber
      (letter-spacing 3px) · `▲ +340% / 27m` red bold · `(2.5→3→5→11)` dim.
    - **containment** — dotted `··········` + `0% — uncontained` (or filled bar + `%`).
    - **modified** — `2026-06-30 00:44` + dim `· 4m ago`.
    - **status** — `ACTIVE` pill: black `#1a1306` on amber `#e3b341`, bold, radius 3px (inactive: dim).
    - **resources** — `📻 3 feeds` `📷 2 cams` (cyan) `🔥 1 fps run` (red).
  - **Live camera (right column, flex:1, min-width 360px)**, column layout, gap 6px:
    - Frame: full width, height ~288px, 1px border `#2a323c`, radius 3px. (Placeholder is a diagonal
      hatch; real impl renders the newest camera frame via the image protocol — see Map §D.)
      Bottom caption overlay on a `transparent→rgba(0,0,0,.7)` gradient: `▶ live cam · Palomar S · 00:42 ago`.
    - Caption line 1: `ALERTWest · cam #4412` (`#9aa4b0`, 12px).
    - Caption line 2: `press i fullscreen · press c all cams` (dim, the `i`/`c` keys amber `#e3b341`).
- **Tab bar**: flex, bottom border `#1c2128`, 13px. Tabs: `Updates (4) ●` (green dot = live),
  `Radio (3)`, `Map`, `Evac (2)` (Evac label red `#ff6a5f`). Active tab shows a 2px underline
  (`#58c5ff`; red `#ff6a5f` under Evac). Inactive labels `#9aa4b0`, hover → `#fff`. Click switches panel.
- **Tab panels** (fills remaining height, scrolls):
  - **Updates**: full-width scroll container with a **slim right-flush scrollbar** (track `#11161c`,
    thumb `#3a4654`, 8px). Inner content capped at `max-width: 760px` so text wraps and never reaches
    the scrollbar. Each update:
    - Header (flex, gap 10px): `┌─ <YYYY-MM-DD HH:MM>` cyan `#58c5ff` · `<rel> ago` dim · reporter
      name magenta `#c98bff` bold. A red `NEW` chip (reverse) flags freshly-arrived reports.
    - Optional **chips** row (margin-left 16px, gap 6px, wrap): aircraft chips black on cyan
      `#2aa6c4` (e.g. `type 3 air tankers ×2`, `type 1 helicopter`), resource/role chips black on
      amber `#e3b341` (e.g. `Air Attack`). Aircraft chips may be enriched with model from catalog.
    - Body: leading `│ ` gutter `#283039`, text `#e8eef4`, **wrapped to the column** (this is the
      overflow fix). Trailing meta in dim.
    - Optional image: `└` + ~64×40 thumbnail + `📷 1 image` dim.
  - **Radio**: list of feeds. Each: `ON` pill (black on green `#3fb950`) or `off` pill (`#9aa4b0`
    on `#3a424c`), feed id dim, name `#e8eef4`, and a cyan `▶ broadcastify.com/listen/feed/<id>`
    sub-line (offline feeds show `offline · last heard …`).
  - **Map**: see Map Recommendations §2 (quadrant plot left, flex:1, height ~320px, with crosshair,
    two dashed range rings, N/S/E/W labels, `◎ you` center, fire markers; right: a 210px legend list
    `glyph Name · bearing · dist`).
  - **Evac**: `⛔ EVACUATION ORDERS` (red bold) + zone detail; `⚠ EVACUATION WARNINGS` (amber bold)
    + zone detail; source/footnote dim. Strip HTML from upstream evac fields.
- **Keybind footer** (bottom, 1 row): top border, dim 12px. `NORMAL  j/k move · / filter · ⏎ load ·
  r refresh · L live · i image · t sort · ? help · q quit` (active keys amber).

---

## Threat Scoring (the core new algorithm — implement server-agnostically in the client)

Replace the distance-only sort with a composite score in `[0,100]`, computed per fire from data you
already fetch, and use it as the default `sort_key`. Suggested model (tune weights against real data):

```
proximity   = clamp(1 − distance_km / within_km, 0, 1)          # closer ⇒ higher
size        = clamp(log10(1 + acreage) / log10(1 + SIZE_REF), 0, 1)   # SIZE_REF e.g. 1000 ac
uncontained = 1 − (containment_pct / 100)        # default 1.0 when unknown/None
growth      = 1 + GROWTH_GAIN · recent_acreage_growth_rate       # from acreage history, ≥1
wind        = 1 + WIND_GAIN · normalized_wind_speed              # gusts amplify
bearing     = align(wind_vector, direction_to_assets_or_user)    # 0.5..1.5; >1 when wind drives
                                                                 # spread toward populated/your area
base   = 100 · proximity · (0.4 + 0.6·size) · uncontained        # core danger
score  = clamp( base · growth · wind · bearing, 0, 100 )
```

- **Inputs already present**: `distance` (haversine), `data.acreage`, `data.containment`,
  `is_active`, `date_modified`, report timestamps. **New inputs**: acreage history (diff successive
  report/poll acreages to get `growth`), and wind (from incident data if available, else a weather
  lookup — degrade gracefully to `wind=1, bearing=1` when absent).
- **Tiers** for the `▰` bar + colors: high ≥ 60 red, 20–60 amber, < 20 green, planned/prescribed ≈ 5 dim.
- **Planned/prescribed burns** should floor near 0 regardless of size.
- Keep the existing sort-cycle key `t`; add `threat` to `_SORT_KEYS` and make it the default when
  `--near` is set.

---

## Interactions & Behavior
- **Sort**: `t` cycles `threat → distance → acreage → updated`; `T` reverses. Status bar reflects it.
- **Navigation**: `j/k` (or ↑/↓) move selection; `gg`/`G` top/bottom; `Ctrl-d/u` half-page; PgUp/PgDn.
- **Tabs**: clicking a tab (or its hotkey) swaps the active panel; default `Updates`. Active tab
  underlined; the live green dot stays on the Updates tab while live mode is on.
- **Updates feed scrolls** within its panel; scrollbar is right-flush, content column-capped.
- **Live mode** (`L`): re-polls the selected fire's reports every 30s; new reports get a `NEW` chip
  and a footer toast (`+N new updates`). This is the **escalation/new-fire alerting** signal.
- **Refresh** (`r`): refetch fires + selected reports; auto-refresh honored (min 30s).
- **Filter** (`/`): live substring filter over id+name+address; `n/N` jump matches; matched
  substring underlined in the list.
- **Image** (`i`): fullscreen the newest camera frame (kitty/sixel path); any key dismisses.
- **Radio/Cams** historically `R`/`c`; in this redesign they live as **tabs** — keep the hotkeys as
  tab-jumpers.
- **Resize**: panes reflow by ratio; below ~80 cols hide the detail pane; show a "too small" notice
  under the hard minimum.
- All network work stays **off the UI thread** (Textual workers or the existing thread+queue).

## State Management
Port `_TuiState` largely intact; additions for this design:
- `sort_key` includes `"threat"`; precompute `threat_score` per fire on each fires refresh.
- `active_tab ∈ {updates, radio, map, evac}` (replaces `side_pane` focus toggles).
- `acreage_history[fire_id]` and `distance_history[fire_id]` (append on each poll) → feed sparklines.
- `wind[fire_id]` (speed/gust/bearing) if/when available.
- Existing caches stay: `reports_cache`, `radio_cache`, `cameras_cache`, `fps_cache`, `image_cache`,
  `chip_cache`, `flash_report_ids` (drives the `NEW` chip + escalation toast).

## Design Tokens

### Color
| Role | Hex |
|---|---|
| App background (deepest) | `#070809` |
| Pane background | `#0c0e11` |
| Panel / inset background | `#0f1318`, `#0a0d11` |
| Status bar background | `#11151b` |
| Selected row background | `#16314c` |
| Border (hairline) | `#1c2128` |
| Border (frame/thumb) | `#2a323c` |
| Bar track / muted line | `#283039` |
| Text primary | `#d4dae0` |
| Text bright/emphasis | `#e8eef4`, `#fff` |
| Text dim | `#9aa4b0` |
| Text dimmer / labels | `#6b7681` |
| Text faint (formula) | `#8a929c` |
| Red — high threat / active / escalation | `#ff6a5f` |
| Amber — med threat / warnings / resources | `#e3b341` |
| Green — contained / OK / LIVE | `#3fb950` |
| Cyan — links / timestamps / location | `#58c5ff` |
| Nav cyan — bearing arrows | `#6fb3d6` |
| Magenta — reporter names | `#c98bff` |
| Chip (aircraft) bg / fg | `#2aa6c4` / `#06121a` |
| Chip (resource/role) bg / fg | `#e3b341` / `#1a1306` |
| LIVE / ON pill bg / fg | `#3fb950` / `#06120a` |
| Scrollbar track / thumb / hover | `#11161c` / `#3a4654` / `#4a5867` |

### Typography
- Family: **JetBrains Mono** (monospace), weights 400/500/700.
- Sizes: fire name 23px · status/section ~13–14px · KV values 13.5px · small/captions 11–12px ·
  threat score 16px. Floor for a real terminal is the cell font; keep relative hierarchy.

### Glyphs
- Threat bar: `▰ ▱` · containment bar: `█ ░ ·` · trend sparkline ramp: `▁▂▃▄▅▆▇█` ·
  bearing arrows: `↑ ↗ → ↘ ↓ ↙ ← ↖` · markers: `◎ ◆ ▲ ● ○` · trend deltas: `▲ ▼` ·
  update tree: `┌─ │ └` · status/evac: `● ⛔ ⚠ ▶ ↗`.

### Spacing
- Row padding `3px 12px`; pane padding `8px 16px`; KV col-gap 8px / row-gap 2px; trend-row gap 14px;
  chip gap 6px; tab padding `5px 14px`. (Translate to cell/character units in a real terminal.)

## Assets
- **Font**: JetBrains Mono (Google Fonts) — in a terminal you inherit the user's font; preserve the
  weight/color hierarchy, not the exact face.
- **Camera frames & update thumbnails**: fetched at runtime from WatchDuty cameras/report media via
  `WatchDutyClient` + `images.py`. No static art ships in this design (placeholders are hatch fills).
- **Emoji** `📻 📷 🔥`: used as resource markers; keep or swap for ASCII (`[R] [C] [F]`) if the target
  terminal/font renders emoji poorly.

## Files
- `Wildfire TUI Improved.dc.html` — the hifi design reference (this bundle).
- Source being redesigned (in the team's repo, provided in-conversation): `libwatchduty/tui.py`
  (curses app), with `client.py` (`WatchDutyClient`), `aircraft.py` (chip extraction/enrichment),
  `images.py` (kitty rendering). Reuse the client/aircraft/images modules as-is; the redesign is a
  rewrite of the **view/interaction** layer only.
