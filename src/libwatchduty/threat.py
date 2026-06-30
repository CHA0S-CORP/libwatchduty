"""Physics-informed wildfire threat scoring (Candidate A — ISI-anchored).

Stdlib-only module implementing the winning design described in the
``design_handoff_wildfire_tui`` review notes. The formula is anchored on
Van Wagner's Initial Spread Index (ISI) from the Canadian Fire Weather
Index system (Van Wagner 1987; NRCan / NWCG PMS 437) so that wind is a
first-class driver of the score (the legacy v1 model multiplied a flat
``1 + 0.04 * speed`` term that was silently no-op when the NOAA fetch
returned no wind).

The composite formula::

    threat = clamp(100 * proximity * intensity * uncontained * growth_mul, 0, 100)

with each factor floored to a non-zero value when its input is missing
so a single absent measurement cannot zero or saturate the score. See
the ``compute_threat`` docstring for the per-term derivation.

Design references
-----------------
* ``design_handoff_wildfire_tui/README.md`` — winning candidate writeup.
* Van Wagner, C. E. (1987). *Development and structure of the Canadian
  Forest Fire Weather Index System.* Canadian Forestry Service.
* NWCG PMS 437 — Fire Weather Index quick reference.

Public surface
--------------
* :class:`ThreatFactors` — dataclass returned from :func:`compute_threat`.
* :func:`compute_threat` — pure function, no I/O, NaN/None safe.

Numpy-free. ``math`` + ``dataclasses`` only. No ``requests`` import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import asin, cos, exp, isfinite, isnan, radians, sin, sqrt
from typing import Any, Iterable

__all__ = ["ThreatFactors", "compute_threat"]


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

# ISI ≈ 20 is the upper end of the "extreme" band on the Canadian FWI
# scale (NRCan public bulletins use 19+ as extreme); divide and clamp
# to fold ISI into a unit interval.
_ISI_NORM_REF = 20.0

# Convert raw NOAA wind units into kph for the Van Wagner wind term.
_MPH_TO_KPH = 1.609344
_MPS_TO_KPH = 3.6
_KT_TO_KPH = 1.852

# Cold-started FFMC regression bounds. The Canadian FWI FFMC is
# normally recursive (it carries state day-to-day); we synthesise a
# single-step approximation from instantaneous T/RH/precip so the score
# is stateless. The clamp keeps the moisture transform in its valid
# domain.
_FFMC_MIN = 50.0
_FFMC_MAX = 99.0

# Climatological summer-California ISI median used when NOAA is missing.
# 8 / 20 = 0.4 → matches NRCan's "high" band lower bound.
_ISI_CLIMATOLOGICAL_NORM = 0.4

# Floors for graceful degradation. Documented in the design doc table.
_UNCONTAINED_MISSING = 0.8   # assume mostly uncontained when null/early
_UNCONTAINED_FLOOR = 0.2     # never give a fire a totally free pass
_INTENSITY_FLOOR = 0.25      # so size/uncontained still bite when wx flat
_INTENSITY_GAIN = 0.75       # intensity = floor + gain * isi_norm
_PROXIMITY_MISSING = 0.3

# Growth normalization. d(acres)/dt > 50 ac/hr → 2x; > 150 ac/hr → 3x cap.
_GROWTH_DIVISOR = 50.0
_GROWTH_MAX = 2.0   # additive cap; growth_mul = 1 + min(g, _GROWTH_MAX) → up to 3x

# Planned-burn cap (matches v1 behaviour).
_PLANNED_CAP = 5.0

# Minimum window required to trust a growth-rate estimate.
_GROWTH_MIN_SECONDS = 30 * 60   # 30 minutes
_GROWTH_MIN_SAMPLES = 2

# Earth radius used for haversine (km).
_EARTH_R_KM = 6371.0088


# ---------------------------------------------------------------------------
# dataclass
# ---------------------------------------------------------------------------

@dataclass
class ThreatFactors:
    """Output of :func:`compute_threat`.

    Attributes
    ----------
    score:
        Final composite threat, clamped to ``[0, 100]``.
    components:
        Per-factor breakdown used to construct ``score`` plus the
        intermediate fire-weather terms (FFMC*, ISI, isi_norm, wind kph,
        growth ac/hr, planned flag). Keys are stable so the TUI can
        display them next to v1 component dumps.
    explanation:
        Ordered human-readable strings describing how each factor was
        derived. Empty list when nothing was degraded or capped.
    """

    score: float
    components: dict[str, float] = field(default_factory=dict)
    explanation: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> float | None:
    """Return ``float(value)`` if finite, else ``None``.

    Coerces ints/strs/numpy-like objects. NaN, ``inf``, ``None``, and
    anything else that won't convert returns ``None`` — callers then
    fall back to documented defaults instead of propagating bogus math.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(f) or isnan(f):
        return None
    return f


def _clamp(value: float, lo: float, hi: float) -> float:
    """Pin ``value`` to ``[lo, hi]``. Tiny helper to keep callsites readable."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between ``(lat,lng)`` pairs in km.

    Duplicate of the same helper in :mod:`tui` — kept here so this
    module has zero internal imports and stays trivially testable.
    """
    lat1, lng1 = a
    lat2, lng2 = b
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    h = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    )
    return 2 * _EARTH_R_KM * asin(sqrt(h))


def _wind_kph(wind: dict | None) -> float | None:
    """Pull a wind speed in kph out of a NOAA-shaped dict.

    Accepts any of ``kph`` / ``speed_kph`` / ``mph`` / ``speed`` (legacy
    v1 key, assumed mph) / ``mps`` / ``speed_mps`` / ``kt``. Returns
    ``None`` when no usable number is present so the caller can fall
    back to the climatological ISI.
    """
    if not wind:
        return None
    for key in ("kph", "speed_kph", "wind_kph"):
        v = _safe_float(wind.get(key))
        if v is not None:
            return max(0.0, v)
    for key in ("mph", "speed", "wind_mph"):
        v = _safe_float(wind.get(key))
        if v is not None:
            return max(0.0, v * _MPH_TO_KPH)
    for key in ("mps", "speed_mps"):
        v = _safe_float(wind.get(key))
        if v is not None:
            return max(0.0, v * _MPS_TO_KPH)
    for key in ("kt", "knots"):
        v = _safe_float(wind.get(key))
        if v is not None:
            return max(0.0, v * _KT_TO_KPH)
    return None


def _ffmc_cold_start(
    t_c: float | None, rh: float | None, precip_mm: float | None
) -> float | None:
    """Single-step regression for the Fine Fuel Moisture Code.

    Substitutes the recursive FFMC update from Van Wagner 1987 with a
    stateless linear approximation that depends only on instantaneous
    point-forecast values::

        FFMC* = clamp(85 + 0.4*(T-20) - 0.5*(RH-40) - 8*precip, 50, 99)

    Returns ``None`` when *all* three inputs are missing — partial data
    is still useful (the missing term defaults to its anchor value).
    """
    if t_c is None and rh is None and precip_mm is None:
        return None
    t = 20.0 if t_c is None else t_c
    h = 40.0 if rh is None else rh
    p = 0.0 if precip_mm is None else max(0.0, precip_mm)
    raw = 85.0 + 0.4 * (t - 20.0) - 0.5 * (h - 40.0) - 8.0 * p
    return _clamp(raw, _FFMC_MIN, _FFMC_MAX)


def _isi_from_ffmc_wind(ffmc_star: float, wind_kph: float) -> float:
    """Van Wagner 1987 Initial Spread Index.

    ``ISI = 0.208 * f_wind * f_F`` with::

        m      = 147.2 * (101 - FFMC*) / (59.5 + FFMC*)
        f_F    = 91.9 * exp(-0.1386 * m) * (1 + m**5.31 / 49_300_000)
        f_wind = exp(0.05039 * wind_kph)

    Returns the raw (un-normalized) ISI. Inputs are already validated /
    clamped by the caller.
    """
    m = 147.2 * (101.0 - ffmc_star) / (59.5 + ffmc_star)
    f_F = 91.9 * exp(-0.1386 * m) * (1.0 + (m ** 5.31) / 49_300_000.0)
    f_wind = exp(0.05039 * wind_kph)
    return 0.208 * f_wind * f_F


def _isi_norm(
    wind: dict | None,
    *,
    temperature_c: float | None = None,
    humidity: float | None = None,
    precip_mm: float | None = None,
) -> tuple[float, dict[str, float], str | None]:
    """Compute normalized ISI in ``[0, 1]`` + raw components.

    Returns ``(isi_norm, raw_terms, note)``. ``note`` is non-None when
    we degraded to the climatological default — the caller stitches it
    into the explanation list.
    """
    w_kph = _wind_kph(wind)
    if wind is not None:
        if temperature_c is None:
            temperature_c = _safe_float(wind.get("temperature_c"))
            if temperature_c is None:
                t_f = _safe_float(wind.get("temperature_f"))
                if t_f is not None:
                    temperature_c = (t_f - 32.0) * 5.0 / 9.0
        if humidity is None:
            humidity = _safe_float(wind.get("humidity") or wind.get("rh"))
        if precip_mm is None:
            precip_mm = _safe_float(
                wind.get("precip_mm") or wind.get("precipitation_mm")
            )

    ffmc = _ffmc_cold_start(temperature_c, humidity, precip_mm)
    if w_kph is None or ffmc is None:
        return (
            _ISI_CLIMATOLOGICAL_NORM,
            {"isi": 0.0, "ffmc_star": 0.0, "wind_kph": w_kph or 0.0},
            "no NOAA wx → climatological ISI",
        )
    isi = _isi_from_ffmc_wind(ffmc, w_kph)
    return (
        _clamp(isi / _ISI_NORM_REF, 0.0, 1.0),
        {"isi": isi, "ffmc_star": ffmc, "wind_kph": w_kph},
        None,
    )


def _growth_rate_per_hour(
    history: Iterable[tuple[float, float]] | None,
) -> float | None:
    """Compute Δacres/Δhours from the longest available time window.

    Skips when the history has fewer than ``_GROWTH_MIN_SAMPLES`` points
    or spans less than ``_GROWTH_MIN_SECONDS`` of wall-clock time. Picks
    the oldest and newest valid samples so transient zeros mid-series
    don't kill the estimate.
    """
    if history is None:
        return None
    samples: list[tuple[float, float]] = []
    for entry in history:
        if not entry or len(entry) < 2:
            continue
        ts = _safe_float(entry[0])
        acres = _safe_float(entry[1])
        if ts is None or acres is None:
            continue
        samples.append((ts, acres))
    if len(samples) < _GROWTH_MIN_SAMPLES:
        return None
    samples.sort(key=lambda r: r[0])
    t0, a0 = samples[0]
    t1, a1 = samples[-1]
    dt_seconds = t1 - t0
    if dt_seconds < _GROWTH_MIN_SECONDS:
        return None
    dt_hours = dt_seconds / 3600.0
    rate = (a1 - a0) / dt_hours
    if rate <= 0:
        return 0.0
    return rate


def _is_planned(fire: dict) -> bool:
    """Prescribed/planned-burn detector mirroring the v1 TUI heuristic."""
    if not isinstance(fire, dict):
        return False
    data = fire.get("data") if isinstance(fire.get("data"), dict) else {}
    if data.get("is_prescribed") or fire.get("is_prescribed"):
        return True
    name = str(fire.get("name") or "").lower()
    return "prescribed" in name or "planned" in name


def _resolve_distance(
    fire: dict,
    *,
    distance_km: float | None,
    near: tuple[float, float] | None,
) -> float | None:
    """Pick a distance: caller-provided wins, fallback to haversine."""
    d = _safe_float(distance_km)
    if d is not None and d >= 0:
        return d
    if near is None:
        return None
    lat = _safe_float(fire.get("lat"))
    lng = _safe_float(fire.get("lng"))
    if lat is None or lng is None:
        return None
    return _haversine_km(near, (lat, lng))


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def compute_threat(
    fire: dict,
    *,
    distance_km: float | None,
    within_km: float,
    acreage_history: Iterable[tuple[float, float]] | None = None,
    wind: dict | None = None,
    near: tuple[float, float] | None = None,
    temperature_c: float | None = None,
    humidity: float | None = None,
    precip_mm: float | None = None,
) -> ThreatFactors:
    """Compute the Candidate A composite threat score for a single fire.

    The formula is::

        threat = 100 * proximity * intensity * uncontained * growth_mul

    each clamped per the design doc (see module docstring). All inputs
    are validated through :func:`_safe_float`; ``None`` / ``NaN`` /
    non-numeric values trip the documented graceful-degradation defaults
    rather than raising.

    Parameters
    ----------
    fire:
        Watch Duty geo-event dict (must at minimum carry ``data`` and
        optionally ``lat``/``lng`` for the haversine fallback).
    distance_km:
        Pre-computed distance from the user's anchor point. Set to
        ``None`` to force the haversine fallback against ``near``.
    within_km:
        Soft cutoff: a fire at exactly ``within_km`` scores
        ``proximity = 0`` (and therefore ``threat = 0``).
    acreage_history:
        Iterable of ``(unix_ts, acres)`` snapshots. The function uses
        the oldest and newest samples to compute Δacres/Δhours; <2
        samples or <30 min window → ``growth_mul = 1``.
    wind:
        NOAA-shaped dict with any of ``kph`` / ``mph`` / ``mps`` / ``kt``
        plus optional ``temperature_c`` / ``humidity`` / ``precip_mm``.
        Missing → climatological ISI.
    near:
        ``(lat, lng)`` of the user's anchor — only used when
        ``distance_km`` is missing.
    temperature_c, humidity, precip_mm:
        Override values used to feed the FFMC cold-start regression.
        When omitted we look inside ``wind`` for the same keys.

    Returns
    -------
    ThreatFactors
        ``score`` in ``[0, 100]`` plus a ``components`` dict and an
        ``explanation`` list of degradation / cap notes.
    """
    if not isinstance(fire, dict):
        return ThreatFactors(score=0.0, components={}, explanation=["bad input"])

    explanation: list[str] = []

    # --- proximity ---------------------------------------------------------
    w_km = _safe_float(within_km)
    if w_km is None or w_km <= 0:
        proximity = _PROXIMITY_MISSING
        explanation.append("invalid within_km → proximity floor 0.3")
    else:
        d_km = _resolve_distance(fire, distance_km=distance_km, near=near)
        if d_km is None:
            proximity = _PROXIMITY_MISSING
            explanation.append("no distance → proximity floor 0.3")
        else:
            proximity = _clamp(1.0 - (d_km / w_km), 0.0, 1.0)

    # --- intensity (ISI-anchored) -----------------------------------------
    isi_norm, isi_raw, isi_note = _isi_norm(
        wind,
        temperature_c=temperature_c,
        humidity=humidity,
        precip_mm=precip_mm,
    )
    if isi_note:
        explanation.append(isi_note)
    intensity = _INTENSITY_FLOOR + _INTENSITY_GAIN * isi_norm

    # --- uncontained ------------------------------------------------------
    data = fire.get("data") if isinstance(fire.get("data"), dict) else {}
    cont = _safe_float(data.get("containment"))
    if cont is None:
        uncontained = _UNCONTAINED_MISSING
        explanation.append("no containment → assume 0.8 uncontained")
    else:
        uncontained = _clamp(1.0 - (cont / 100.0), _UNCONTAINED_FLOOR, 1.0)

    # --- growth multiplier ------------------------------------------------
    rate = _growth_rate_per_hour(acreage_history)
    if rate is None:
        growth_mul = 1.0
        growth_rate = 0.0
        explanation.append("no usable acreage history → growth ×1")
    else:
        growth_rate = rate
        growth_mul = 1.0 + _clamp(rate / _GROWTH_DIVISOR, 0.0, _GROWTH_MAX)

    # --- combine + planned cap --------------------------------------------
    score = 100.0 * proximity * intensity * uncontained * growth_mul
    score = _clamp(score, 0.0, 100.0)

    planned = _is_planned(fire)
    if planned and score > _PLANNED_CAP:
        explanation.append("planned/prescribed burn → score capped at 5")
        score = _PLANNED_CAP

    components = {
        "score": score,
        "proximity": proximity,
        "intensity": intensity,
        "isi_norm": isi_norm,
        "isi": isi_raw["isi"],
        "ffmc_star": isi_raw["ffmc_star"],
        "wind_kph": isi_raw["wind_kph"],
        "uncontained": uncontained,
        "growth_mul": growth_mul,
        "growth_rate": growth_rate,
        "planned": 1.0 if planned else 0.0,
    }
    return ThreatFactors(
        score=score, components=components, explanation=explanation
    )
