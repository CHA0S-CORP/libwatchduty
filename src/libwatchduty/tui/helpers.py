"""Small pure helpers: text, geo math, glyph pickers, threat scoring.

Where a shared twin exists in :mod:`libwatchduty._geo` /
:mod:`libwatchduty._text`, the tui-local name is a thin alias onto it so
existing call sites (and tests reaching through the facade) keep working.
"""

from __future__ import annotations

import locale
from math import log10
from typing import Any

from .. import threat as _threat_mod
from .._geo import haversine_km, initial_bearing
from .._text import format_age, seconds_since_iso, split_html_lines, strip_html
from .state import (
    _ARROWS,
    _COMPASS,
    _GROWTH_GAIN,
    _SIZE_REF,
    _SPARK_RAMP,
    _THREAT_EMPTY,
    _THREAT_FULL,
    _WIND_GAIN,
)

# Thin aliases onto the shared helpers (see module docstring).
_strip_html = strip_html
_split_html_lines = split_html_lines
_haversine_km = haversine_km
_initial_bearing = initial_bearing
_seconds_since_iso = seconds_since_iso
_format_age = format_age


def _bearing_arrow(deg: float | None) -> str:
    """Pick an 8-point compass arrow glyph."""
    if deg is None:
        return "·"
    idx = int(((deg % 360.0) / 45.0) + 0.5) % 8
    return _ARROWS[idx]


def _bearing_compass(deg: float | None) -> str:
    """8-point compass label (`N`, `NE`, …)."""
    if deg is None:
        return ""
    idx = int(((deg % 360.0) / 45.0) + 0.5) % 8
    return _COMPASS[idx]


def _wrap_around_image(
    text: str, narrow_w: int, wide_w: int, narrow_rows: int,
) -> list[str]:
    """Greedy word-wrap: first ``narrow_rows`` lines use ``narrow_w``
    (so they fit beside an inline image), the rest use ``wide_w``.
    Returns at least one (possibly empty) line."""
    if not text or not text.strip():
        return [""]
    words = text.split()
    lines: list[str] = []
    cur = ""

    def cap() -> int:
        return narrow_w if len(lines) < narrow_rows else wide_w

    for w in words:
        if not cur:
            cur = w
            continue
        if len(cur) + 1 + len(w) <= cap():
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _sparkline(values: list[float], width: int = 4) -> str:
    """Map ``values`` to the `▁▂▃▄▅▆▇█` ramp, last ``width`` samples."""
    if not values or width <= 0:
        return ""
    vs = list(values)[-width:]
    lo, hi = min(vs), max(vs)
    if hi == lo:
        return _SPARK_RAMP[3] * len(vs)
    rng = hi - lo
    return "".join(
        _SPARK_RAMP[min(7, int(round((v - lo) / rng * 7)))] for v in vs
    )


def _safe_str(s: Any) -> str:
    """Coerce to a printable string the locale can render."""
    if s is None:
        return ""
    text = str(s)
    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        text.encode(enc)
        return text
    except UnicodeEncodeError:
        return text.encode("ascii", "replace").decode("ascii")


def _is_planned(fire: dict) -> bool:
    """Heuristic: prescribed/planned burn → keep threat near zero."""
    data = fire.get("data") or {}
    if data.get("is_prescribed") or fire.get("is_prescribed"):
        return True
    name = (fire.get("name") or "").lower()
    return "prescribed" in name or "planned" in name


# ---------------------------------------------------------------------------
# threat scoring
# ---------------------------------------------------------------------------

def _threat_factors(
    fire: dict,
    *,
    distance_km: float | None,
    within_km: float,
    acreage_hist: list[tuple[float, float]],
    wind: dict | None,
    near: tuple[float, float] | None,
    model: str = "v1",
) -> dict[str, float]:
    """Compute composite threat factors per README formula.

    Returns ``{score, proximity, size, uncontained, growth, growth_rate,
    wind, bearing, planned}`` — score is the final clamped [0,100] value.
    Degrades when wind/growth absent (those multipliers stay at 1.0).

    When ``model == "v2"`` the work is delegated to
    :func:`libwatchduty.threat.compute_threat` (Candidate A — ISI-anchored
    physics-informed scoring). v1 keeps the original size/wind-gain
    multiplicative formula for backwards compatibility.
    """
    if model == "v2":
        tf = _threat_mod.compute_threat(
            fire,
            distance_km=distance_km,
            within_km=within_km,
            acreage_history=acreage_hist or [],
            wind=wind,
            near=near,
        )
        c = tf.components
        # Map the v2 component dict back onto the v1-shaped surface that
        # the rest of the TUI consumes (sort, KV table, sparkline). The
        # `intensity` term replaces the v1 `size` slot, and `wind` /
        # `bearing` collapse into the ISI-anchored intensity (they're
        # already baked in there), so we expose them as 1.0 placeholders
        # to keep callers that index by name happy.
        return {
            "score": float(c.get("score", tf.score)),
            "proximity": float(c.get("proximity", 0.0)),
            "size": float(c.get("intensity", 0.0)),
            "uncontained": float(c.get("uncontained", 0.0)),
            "growth": float(c.get("growth_mul", 1.0)),
            "growth_rate": float(c.get("growth_rate", 0.0)),
            "wind": float(c.get("wind_kph", 0.0)),
            "bearing": 1.0,
            "planned": float(c.get("planned", 0.0)),
            "isi": float(c.get("isi", 0.0)),
            "isi_norm": float(c.get("isi_norm", 0.0)),
            "ffmc_star": float(c.get("ffmc_star", 0.0)),
        }

    data = fire.get("data") or {}
    try:
        acres = float(data.get("acreage") or 0)
    except (TypeError, ValueError):
        acres = 0.0

    if distance_km is None or within_km <= 0:
        proximity = 0.3
    else:
        proximity = max(0.0, min(1.0, 1.0 - (distance_km / within_km)))

    size = max(0.0, min(1.0, log10(1.0 + acres) / log10(1.0 + _SIZE_REF)))

    cont = data.get("containment")
    if isinstance(cont, (int, float)):
        uncontained = max(0.0, 1.0 - (float(cont) / 100.0))
    else:
        uncontained = 1.0

    growth_rate = 0.0
    growth_mul = 1.0
    if acreage_hist and len(acreage_hist) >= 2:
        oldest = max(1e-3, float(acreage_hist[0][1]))
        newest = float(acreage_hist[-1][1])
        if newest > oldest:
            growth_rate = (newest - oldest) / oldest
            growth_mul = 1.0 + _GROWTH_GAIN * growth_rate

    wind_mul = 1.0
    bearing_mul = 1.0
    if wind:
        try:
            speed = float(wind.get("speed") or wind.get("mph") or 0)
        except (TypeError, ValueError):
            speed = 0.0
        wind_mul = 1.0 + _WIND_GAIN * speed
        wb = wind.get("bearing")
        lat, lng = fire.get("lat"), fire.get("lng")
        if (
            wb is not None
            and near is not None
            and isinstance(lat, (int, float))
            and isinstance(lng, (int, float))
        ):
            target = _initial_bearing((float(lat), float(lng)), near)
            diff = abs(((float(wb) - target + 540.0) % 360.0) - 180.0)
            bearing_mul = max(0.5, min(1.5, 1.5 - (diff / 180.0)))

    base = 100.0 * proximity * (0.4 + 0.6 * size) * uncontained
    score = max(0.0, min(100.0, base * growth_mul * wind_mul * bearing_mul))

    planned = _is_planned(fire)
    if planned:
        score = min(score, 5.0)

    return {
        "score": score,
        "proximity": proximity,
        "size": size,
        "uncontained": uncontained,
        "growth": growth_mul,
        "growth_rate": growth_rate,
        "wind": wind_mul,
        "bearing": bearing_mul,
        "planned": 1.0 if planned else 0.0,
    }


def _threat_tier(score: float | None) -> str:
    """Tier role name: `red` / `amber` / `green` / `dimmer`."""
    if score is None:
        return "dimmer"
    if score >= 60:
        return "red"
    if score >= 20:
        return "amber"
    if score < 6:
        return "dimmer"
    return "green"


def _threat_bar_glyphs(score: float | None) -> str:
    """3-segment ▰▱ bar string."""
    if score is None:
        return _THREAT_EMPTY * 3
    s = max(0.0, min(100.0, float(score)))
    if s >= 60:
        n = 3
    elif s >= 30:
        n = 2
    elif s >= 10:
        n = 1
    else:
        n = 0
    return _THREAT_FULL * n + _THREAT_EMPTY * (3 - n)
