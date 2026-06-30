"""Tests for the Candidate A (ISI-anchored) threat scorer.

Covers the contract advertised in ``src/libwatchduty/threat.py``:

* monotonicity in distance, size (via uncontained proxy), containment
* graceful degradation when wind / NOAA / acreage history are missing
* planned-burn cap stays at 5
* NaN / None / non-numeric inputs do not raise on any field
"""

from __future__ import annotations

import math
import time

import pytest

from libwatchduty import compute_threat
from libwatchduty.threat import (
    ThreatFactors,
    _ffmc_cold_start,
    _growth_rate_per_hour,
    _isi_from_ffmc_wind,
    _wind_kph,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _fire(
    *,
    lat: float = 34.0,
    lng: float = -117.0,
    acreage: float = 1000.0,
    containment: float | None = 25.0,
    name: str = "Test Fire",
    is_prescribed: bool = False,
) -> dict:
    return {
        "id": 1,
        "name": name,
        "lat": lat,
        "lng": lng,
        "is_prescribed": is_prescribed,
        "data": {"acreage": acreage, "containment": containment},
    }


def _hot_wind() -> dict:
    """Summer-CA-ish weather feeding ISI: T=30C, RH=20%, W=25 kph."""
    return {"kph": 25.0, "temperature_c": 30.0, "humidity": 20.0, "precip_mm": 0.0}


def _hist(now: float, *, span_hours: float, growth_acres: float) -> list[tuple[float, float]]:
    """Two acreage samples ``span_hours`` apart with ``growth_acres`` gain."""
    return [
        (now - span_hours * 3600.0, 1000.0),
        (now, 1000.0 + growth_acres),
    ]


# ---------------------------------------------------------------------------
# basic contract
# ---------------------------------------------------------------------------

def test_returns_threatfactors_dataclass():
    out = compute_threat(_fire(), distance_km=10.0, within_km=50.0)
    assert isinstance(out, ThreatFactors)
    assert isinstance(out.components, dict)
    assert isinstance(out.explanation, list)
    assert 0.0 <= out.score <= 100.0


def test_components_have_expected_keys():
    out = compute_threat(_fire(), distance_km=10.0, within_km=50.0, wind=_hot_wind())
    for k in (
        "score", "proximity", "intensity", "isi_norm", "isi", "ffmc_star",
        "wind_kph", "uncontained", "growth_mul", "growth_rate", "planned",
    ):
        assert k in out.components, f"missing component {k}"


# ---------------------------------------------------------------------------
# monotonicity
# ---------------------------------------------------------------------------

def test_closer_fire_scores_higher_all_else_equal():
    """Proximity is a multiplicative term — halving distance must raise score."""
    near_out = compute_threat(_fire(), distance_km=2.0, within_km=50.0, wind=_hot_wind())
    far_out = compute_threat(_fire(), distance_km=40.0, within_km=50.0, wind=_hot_wind())
    assert near_out.score > far_out.score


def test_size_monotonicity_via_uncontained_proxy():
    """The v2 model intentionally drops raw size in favour of ISI + growth.

    The closest stand-in for "bigger fire scores higher all else equal"
    in this formulation is acreage growth rate: a fire actively growing
    must score higher than one that's flat. (Per the design doc: "a
    100k-ac mopped-up fire is less threatening than a 500-ac runaway".)
    """
    now = time.time()
    fire = _fire(containment=10.0)
    flat = compute_threat(
        fire,
        distance_km=5.0, within_km=50.0,
        acreage_history=_hist(now, span_hours=2.0, growth_acres=0.0),
        wind=_hot_wind(),
    )
    growing = compute_threat(
        fire,
        distance_km=5.0, within_km=50.0,
        acreage_history=_hist(now, span_hours=2.0, growth_acres=400.0),
        wind=_hot_wind(),
    )
    assert growing.score > flat.score


def test_more_contained_lowers_score():
    base_kwargs = dict(distance_km=5.0, within_km=50.0, wind=_hot_wind())
    low_cont = compute_threat(_fire(containment=5.0), **base_kwargs)
    high_cont = compute_threat(_fire(containment=95.0), **base_kwargs)
    assert low_cont.score > high_cont.score


def test_containment_strict_monotonicity_sweep():
    """Walk containment 0 → 100 and assert score is non-increasing."""
    scores = []
    for c in (0, 10, 25, 50, 75, 90, 100):
        out = compute_threat(
            _fire(containment=float(c)),
            distance_km=5.0, within_km=50.0, wind=_hot_wind(),
        )
        scores.append(out.score)
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1] - 1e-9, scores


def test_wind_drives_intensity_up():
    """Higher wind → higher ISI → higher intensity → higher score."""
    calm = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        wind={"kph": 2.0, "temperature_c": 30.0, "humidity": 20.0, "precip_mm": 0.0},
    )
    windy = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        wind={"kph": 40.0, "temperature_c": 30.0, "humidity": 20.0, "precip_mm": 0.0},
    )
    assert windy.score > calm.score


# ---------------------------------------------------------------------------
# graceful degradation
# ---------------------------------------------------------------------------

def test_no_wind_no_history_does_not_raise():
    out = compute_threat(_fire(), distance_km=5.0, within_km=50.0)
    assert 0.0 <= out.score <= 100.0
    # Climatological ISI default kicks in.
    assert any("climatological" in note.lower() for note in out.explanation)
    # Growth defaults to 1x.
    assert out.components["growth_mul"] == pytest.approx(1.0)


def test_missing_containment_uses_documented_floor():
    out = compute_threat(
        _fire(containment=None), distance_km=5.0, within_km=50.0, wind=_hot_wind()
    )
    assert out.components["uncontained"] == pytest.approx(0.8)
    assert any("containment" in n for n in out.explanation)


def test_missing_distance_uses_proximity_floor():
    out = compute_threat(_fire(), distance_km=None, within_km=50.0, wind=_hot_wind())
    assert out.components["proximity"] == pytest.approx(0.3)


def test_missing_distance_falls_back_to_haversine_when_near_provided():
    # Far enough that proximity is strictly less than the 0.3 floor would give.
    out = compute_threat(
        _fire(lat=34.0, lng=-117.0),
        distance_km=None, within_km=50.0,
        near=(34.05, -117.05),
        wind=_hot_wind(),
    )
    assert out.components["proximity"] > 0.3   # very close → near 1.0


def test_history_too_short_disables_growth():
    now = time.time()
    short = [(now - 60.0, 1000.0), (now, 2000.0)]  # only 1 min window
    out = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        acreage_history=short, wind=_hot_wind(),
    )
    assert out.components["growth_mul"] == pytest.approx(1.0)


def test_growth_cap_at_3x():
    """A wildly growing fire's growth_mul tops out at 3.0 (i.e. +2.0 over 1)."""
    now = time.time()
    runaway = [(now - 3600.0, 1000.0), (now, 1000.0 + 500.0)]   # 500 ac/hr
    out = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        acreage_history=runaway, wind=_hot_wind(),
    )
    assert out.components["growth_mul"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# planned burn cap
# ---------------------------------------------------------------------------

def test_planned_burn_is_capped_at_5():
    fire = _fire(name="Prescribed burn at Acme", is_prescribed=True)
    out = compute_threat(fire, distance_km=1.0, within_km=50.0, wind=_hot_wind())
    assert out.score <= 5.0
    assert any("planned" in n.lower() or "prescribed" in n.lower() for n in out.explanation)


def test_planned_burn_name_only_also_capped():
    fire = _fire(name="Big Planned Burn 2026")
    out = compute_threat(fire, distance_km=1.0, within_km=50.0, wind=_hot_wind())
    assert out.score <= 5.0


# ---------------------------------------------------------------------------
# NaN / None / garbage safety
# ---------------------------------------------------------------------------

def test_nan_distance_uses_floor():
    out = compute_threat(_fire(), distance_km=float("nan"), within_km=50.0)
    assert math.isfinite(out.score)
    assert 0.0 <= out.score <= 100.0


def test_nan_within_km_does_not_raise():
    out = compute_threat(_fire(), distance_km=5.0, within_km=float("nan"))
    assert math.isfinite(out.score)


def test_infinite_within_km_handled():
    out = compute_threat(_fire(), distance_km=5.0, within_km=float("inf"))
    assert math.isfinite(out.score)


def test_garbage_acreage_history_does_not_raise():
    bad = [
        ("not-a-number", "nope"),
        (None, None),
        (time.time(), float("nan")),
        (),
    ]
    out = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        acreage_history=bad, wind=_hot_wind(),
    )
    assert math.isfinite(out.score)
    assert out.components["growth_mul"] == pytest.approx(1.0)


def test_garbage_wind_dict_does_not_raise():
    out = compute_threat(
        _fire(),
        distance_km=5.0, within_km=50.0,
        wind={"kph": "fast", "temperature_c": None, "humidity": float("nan")},
    )
    assert math.isfinite(out.score)
    # No valid wind speed → climatological fallback fires.
    assert any("climatological" in n.lower() for n in out.explanation)


def test_garbage_containment_falls_back_to_default():
    fire = _fire(containment=None)
    fire["data"]["containment"] = "n/a"
    out = compute_threat(fire, distance_km=5.0, within_km=50.0, wind=_hot_wind())
    assert math.isfinite(out.score)
    assert out.components["uncontained"] == pytest.approx(0.8)


def test_none_fire_returns_zero_score_safely():
    # Wrong-type input shouldn't crash the renderer.
    out = compute_threat(None, distance_km=5.0, within_km=50.0)  # type: ignore[arg-type]
    assert out.score == 0.0


def test_missing_lat_lng_with_near_falls_back_to_proximity_floor():
    fire = {"id": 1, "data": {"acreage": 100.0, "containment": 10.0}}
    out = compute_threat(
        fire, distance_km=None, within_km=50.0, near=(34.0, -117.0),
        wind=_hot_wind(),
    )
    assert out.components["proximity"] == pytest.approx(0.3)


def test_negative_within_km_uses_proximity_floor():
    out = compute_threat(_fire(), distance_km=5.0, within_km=-1.0, wind=_hot_wind())
    assert out.components["proximity"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# unit conversions
# ---------------------------------------------------------------------------

def test_wind_kph_accepts_mph():
    assert _wind_kph({"mph": 10.0}) == pytest.approx(16.0934, abs=1e-3)


def test_wind_kph_accepts_mps():
    assert _wind_kph({"mps": 10.0}) == pytest.approx(36.0)


def test_wind_kph_accepts_knots():
    assert _wind_kph({"kt": 10.0}) == pytest.approx(18.52)


def test_wind_kph_prefers_explicit_kph_over_mph():
    # If both keys are present the explicit kph value wins.
    assert _wind_kph({"kph": 100.0, "mph": 10.0}) == pytest.approx(100.0)


def test_wind_kph_handles_empty_and_none():
    assert _wind_kph(None) is None
    assert _wind_kph({}) is None
    assert _wind_kph({"mph": None, "kph": "bogus"}) is None


# ---------------------------------------------------------------------------
# internal physics smoke
# ---------------------------------------------------------------------------

def test_ffmc_cold_start_clamps_high_for_hot_dry():
    ffmc = _ffmc_cold_start(t_c=40.0, rh=5.0, precip_mm=0.0)
    assert 90.0 <= ffmc <= 99.0


def test_ffmc_cold_start_clamps_low_for_wet_humid():
    ffmc = _ffmc_cold_start(t_c=5.0, rh=100.0, precip_mm=10.0)
    assert 50.0 <= ffmc <= 70.0


def test_ffmc_returns_none_when_all_missing():
    assert _ffmc_cold_start(None, None, None) is None


def test_isi_grows_monotonically_with_wind():
    ffmc = 90.0
    isi_low = _isi_from_ffmc_wind(ffmc, 0.0)
    isi_hi = _isi_from_ffmc_wind(ffmc, 50.0)
    assert isi_hi > isi_low > 0


def test_growth_rate_returns_none_below_threshold():
    now = time.time()
    # 10 minutes only → below the 30-min floor.
    assert _growth_rate_per_hour([(now - 600, 100.0), (now, 200.0)]) is None


def test_growth_rate_uses_longest_window():
    now = time.time()
    # Three samples; the rate over 2h should be (1000-200)/2 = 400 ac/hr.
    rate = _growth_rate_per_hour([
        (now - 7200.0, 200.0),
        (now - 3600.0, 500.0),
        (now, 1000.0),
    ])
    assert rate == pytest.approx(400.0)
