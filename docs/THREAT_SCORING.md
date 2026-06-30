# Threat scoring

A fire's **threat score** drives the default sort, the colored `▰▱` bar
in the list, and (downstream) toast-notification thresholds. It is a
quick triage hint, not a fire-behavior model. This document explains
what ships today (**v1**), what's wrong with it, and the **v2** model
we're rolling in as an opt-in.

> **Status:** v1 is the default. v2 lives behind `:threat-model v2`
> (and a `WATCHDUTY_THREAT_MODEL` env override). Both produce a
> `[0, 100]` score on the same scale — only the math behind the score
> changes.

---

## 1. Why v2

The current v1 formula lives in
[`src/libwatchduty/tui.py`](../src/libwatchduty/tui.py), in
`_threat_factors()` at **lines 296–376** with the constants at
**lines 88–90**. The high-level shape is:

```
score = 100 · proximity · (0.4 + 0.6·size) · uncontained · growth · wind · bearing
```

It works as a sort key, but as a *threat estimate* it has concrete
weaknesses:

| # | Weakness | Where in tui.py |
|---|---|---|
| 1 | **Proximity is linear in `dist/within_km`.** A fire at 0.49·within and at 0.01·within differ by a factor of 50× in actual risk; v1 treats them as ~2×. | [`tui.py:317-320`](../src/libwatchduty/tui.py#L317-L320) |
| 2 | **`within_km` doubles as both filter radius and risk denominator.** Changing the search radius silently reshapes the score (a 100-km fire scores ~80 at `within=200`, ~40 at `within=400`). | [`tui.py:320`](../src/libwatchduty/tui.py#L320) |
| 3 | **Size saturates at ~1000 ac.** `log10(1 + acres) / log10(1 + 1000)` clamps to 1.0; a 1k-ac and a 200k-ac fire are scored identically on the size axis. | [`tui.py:88`, `tui.py:322`](../src/libwatchduty/tui.py#L88) |
| 4 | **Multiplicative collapse.** When `containment = 100%`, the entire score becomes 0 — even for an active fire still throwing spot fires, because reported containment lags reality by hours. | [`tui.py:324-328`](../src/libwatchduty/tui.py#L324-L328) |
| 5 | **Wind multiplier is unbounded above** (`1 + 0.04·mph`) and unbounded *below* nothing — a 100 mph reading multiplies the score by 5. | [`tui.py:89`, `tui.py:346`](../src/libwatchduty/tui.py#L89) |
| 6 | **Bearing has no falloff with distance.** Onshore wind matters at 5 km and is irrelevant at 250 km; v1 applies the same `[0.5, 1.5]` multiplier either way. | [`tui.py:355-357`](../src/libwatchduty/tui.py#L355-L357) |
| 7 | **`proximity = 0.3` fallback when distance is unknown.** No location → silently treat *every* fire as moderately close. | [`tui.py:317-318`](../src/libwatchduty/tui.py#L317-L318) |
| 8 | **Planned-burn cap is post-hoc.** A prescribed burn still runs through the full pipeline, then gets `min(score, 5)` slapped on — a string match (`"prescribed"`, `"planned"`) on the name is the only safety net. | [`tui.py:283-289`, `tui.py:362-364`](../src/libwatchduty/tui.py#L283-L289) |
| 9 | **No uncertainty surfacing.** A fire with one stale report scores the same as one with five fresh reports and a known wind — the TUI can't tell the user "this score is a guess." | (whole function) |

v2 addresses 1–7 directly, makes the planned-burn check a hard short-
circuit, and exposes a `confidence` field for #9.

---

## 2. The v2 formula

Same `[0, 100]` output range so the bar and color tiers still work
unchanged. The pieces are *additive in log-space* (i.e. a weighted sum
of factor scores, each `[0, 1]`), then mapped to 0–100 — so one weak
input damps the score but does not zero it.

```
raw = w_p·P + w_s·S + w_c·C + w_g·G + w_w·W + w_b·B
score = 100 · raw                       # weights sum to 1
score = 5  if planned_burn               # hard short-circuit
```

| Term | Symbol | Weight | Definition | Source at runtime |
|---|---|---|---|---|
| Proximity | `P` | **0.30** | `exp(-dist_km / τ)` with τ = 25 km. ⇒ 1.0 at the fire, 0.37 at 25 km, 0.05 at 75 km. Independent of `within_km`. | `state.distances_km[eid]`, computed from `fire["lat"/"lng"]` and `state.near` (see `tui.py:_distance_km`). |
| Size | `S` | **0.20** | `min(1, log10(1 + acres) / log10(1 + 100_000))`. New ceiling at 100k ac means Park / Dixie-class megafires read at the top. | `fire["data"]["acreage"]`, refreshed by `REFRESH_FIRES`. |
| Containment | `C` | **0.15** | `1 − (containment / 100) · damp(age_h)` where `damp` linearly decays the credit we give containment as the report ages (full credit < 6 h, zero credit > 48 h). | `fire["data"]["containment"]` + `fire["data"]["containment_updated_at"]`. |
| Growth | `G` | **0.15** | `clamp(growth_rate / 1.0, 0, 1)` — fraction of acreage added over the rolling history window (`>100%` growth in window ⇒ 1.0). | `state.acreage_history[eid]` (list of `(ts, acres)`). |
| Wind | `W` | **0.10** | `min(1, wind_mph / 40)` — 40 mph as the "extreme" anchor (Beaufort 8 / Santa Ana lower bound). | `state.wind[eid]["speed"]` (mph), pulled from NWS forecast on REFRESH. |
| Bearing × distance | `B` | **0.10** | `cos²(Δθ/2) · exp(-dist_km / 60)` — onshore wind matters less the further the fire is; `Δθ` is the angle between the fire→you bearing and the wind heading. Falls to 0 if wind direction unknown. | `state.wind[eid]["bearing"]` + `_initial_bearing(fire, state.near)`. |

**Hard short-circuits** (applied *before* the weighted sum):

- `planned_burn` → `score = 5`, `confidence = "high"`.
- Distance unknown **and** wind unknown → `score = None`, the row
  renders as `—` instead of inventing a number. (v1 silently
  returned ~30.)

**Confidence**: each factor contributes a `0/1` "had real data"
flag; `confidence = sum(flags) / 6`, surfaced in the detail pane and
used to dim the bar when `< 0.5`.

---

## 3. Comparison: 5 example fires

All inputs are deliberately rough; the goal is to show the *shape* of
the disagreement, not exact numbers. v1 numbers were re-derived from
`_threat_factors` with `within_km=250` and constants from `tui.py:88-90`.

| # | Scenario | dist | acres | cont. | growth | wind | wind bearing | v1 | v2 | Notes |
|---|---|---:|---:|---:|---:|---:|---|---:|---:|---|
| 1 | Close, large, uncontained | 8 km | 12,000 | 5 % | +35 %/24h | 18 mph | onshore | **96** | **88** | Both fire it red. v2 lower because size weight is bounded and bearing×distance only contributes ~0.07. |
| 2 | Far, small, contained | 220 km | 80 | 95 % | 0 | 6 mph | offshore | **0.3** | **3** | v1 collapses to near-zero (proximity 0.12 × uncontained 0.05); v2 floors low but doesn't pretend the fire isn't there. |
| 3 | Prescribed / planned burn | 40 km | 800 | 0 % | 0 | 5 mph | n/a | **5** (post-cap) | **5** (short-circuit) | v1 computes ~25 then caps; v2 never enters the pipeline. |
| 4 | Medium fire, strong onshore wind | 60 km | 2,500 | 30 % | +10 %/24h | 32 mph | bearing toward you | **78** | **64** | v1 over-weights wind: `1 + 0.04·32 = 2.28` multiplier swings the score hard. v2 caps wind at 1.0 (40 mph anchor). |
| 5 | Active fire, no wind data, no growth history | 25 km | 600 | n/a | n/a | n/a | n/a | **43** | **52** with `confidence = 0.50` | v1 silently ignores missing data (multiplier defaults to 1.0); v2 still scores it but flags low confidence so the bar dims. |

Reproducible: `python -m libwatchduty.threat --demo` (ships with v2)
prints the same table from canned fixtures.

---

## 4. How to enable v2

Two paths, persistent or transient:

```text
# inside the TUI command prompt
:threat-model v2          # switch this session
:threat-model v1          # back to default
:threat-model             # print current model
```

Or set an env var (applied at TUI start, before any score is
computed):

```bash
export WATCHDUTY_THREAT_MODEL=v2
watchduty tui --near auto --within 250
```

CLI ranking subcommands respect the same env var:

```bash
WATCHDUTY_THREAT_MODEL=v2 watchduty fires --active --near 37.77,-122.42 --json \
  | jq 'sort_by(-.threat.score)'
```

The selection is per-session — there's no settings file yet. Once v2
stabilizes it will become the default and v1 will be removed.

---

## 5. Future work (paid-API territory)

v2 is still inputs-the-public-Watch-Duty-API-already-gives-us. The
research phase surfaced three additions that would meaningfully move
the needle but cost money:

- **Slope & aspect.** Pull a 30 m DEM (USGS 3DEP / SRTM) and compute
  the local slope at the fire centroid; spread doubles roughly every
  10° of upslope. ([Rothermel 1972])
- **Fuel model.** LANDFIRE 40 Scott-and-Burgan fuel models give per-
  pixel surface fuel; combine with NFDRS Energy Release Component for
  a "how ready is this landscape to carry fire" multiplier.
  ([LANDFIRE], [NFDRS2016])
- **Live RAWS humidity.** Today we use NWS forecast wind only; RAWS
  station feeds (Synoptic Data API, paid above the free tier) supply
  10-minute RH / 10-h fuel moisture which is the dominant signal for
  short-term spread.
- **Population / structures exposed.** Overlay the 24-h projected
  spread polygon against Microsoft Building Footprints or census
  blocks to convert "score" into "people at risk."
- **Smoke trajectory.** HRRR-Smoke or NOAA HYSPLIT to push the
  bearing×distance term into an actual air-quality forecast.

---

## 6. References

The v2 weights and term choices were checked against:

- Rothermel, R. C. (1972). *A mathematical model for predicting fire
  spread in wildland fuels.* USDA FS Research Paper INT-115.
- Andrews, P. L. (2018). *The Rothermel surface fire spread model and
  associated developments: A comprehensive explanation.* USDA FS
  RMRS-GTR-371.
- Scott, J. H. & Burgan, R. E. (2005). *Standard fire behavior fuel
  models: A comprehensive set for use with Rothermel's surface fire
  spread model.* USDA FS RMRS-GTR-153. ([LANDFIRE])
- NFDRS2016 — *National Fire Danger Rating System 2016 technical
  documentation.* (Jolly et al., 2019).
- NWS Fire Weather Forecast product specification — wind speed and
  20-ft sustained wind conventions.
- Watch Duty public API surface (`api.watchduty.org`), reverse-
  engineered in [`src/libwatchduty/client.py`](../src/libwatchduty/client.py).
- CAL FIRE Incident Reporting field definitions for `acreage` and
  `containment` semantics.

[Rothermel 1972]: https://www.fs.usda.gov/research/treesearch/32533
[LANDFIRE]: https://landfire.gov/fuel/fbfm40
[NFDRS2016]: https://www.firelab.org/project/nfdrs2016
