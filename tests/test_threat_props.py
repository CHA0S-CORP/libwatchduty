"""Property-style tests for the Candidate A (ISI-anchored) threat scorer.

Complements ``test_threat.py`` (example-based contract tests) with
invariant sweeps over input grids:

* RH sensitivity — bone-dry air (RH 0) must score *higher* than RH 40
  (regression for the falsy-``or`` bug that treated RH 0 as missing)
* precip 0.0 must win over a nonzero ``precipitation_mm`` fallback key
* strict wind → ISI monotonicity; score never drops as wind rises
* score non-increasing as distance grows; exactly ``within_km`` → 0
* score always finite in ``[0, 100]`` across a grid of hostile inputs
* planned-burn cap holds even under worst-case inputs
* growth-rate edge cases (short history, short window, shrinking fire)
* unit parsing — the same physical wind expressed in kph / mph / mps /
  kt yields (approximately) identical scores
"""

from __future__ import annotations

import itertools
import math
import time

import pytest

from libwatchduty import compute_threat
from libwatchduty.threat import _growth_rate_per_hour, _isi_from_ffmc_wind


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _fire(
    *,
    containment: float | None = 25.0,
    name: str = "Prop Fire",
    is_prescribed: bool = False,
) -> dict:
    return {
        "id": 9,
        "name": name,
        "lat": 34.0,
        "lng": -117.0,
        "is_prescribed": is_prescribed,
        "data": {"acreage": 1000.0, "containment": containment},
    }


def _wx(**overrides) -> dict:
    """Moderate baseline weather; keeps ISI well below the norm clamp."""
    base = {"kph": 15.0, "temperature_c": 25.0, "humidity": 40.0, "precip_mm": 0.0}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# RH sensitivity (falsy-or regression)
# ---------------------------------------------------------------------------

def test_rh_zero_scores_higher_than_rh_40():
    """FFMC* rises as RH falls: RH 0 is peak danger, not 'missing'."""
    dry = compute_threat(_fire(), distance_km=5.0, within_km=50.0, wind=_wx(humidity=0.0))
    mid = compute_threat(_fire(), distance_km=5.0, within_km=50.0, wind=_wx(humidity=40.0))
    assert dry.components["ffmc_star"] > mid.components["ffmc_star"]
    assert dry.score > mid.score


def test_humidity_zero_does_not_fall_back_to_rh_key():
    """RH 0 in ``humidity`` must NOT be shadowed by a nonzero ``rh`` key."""
    both = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        wind=_wx(humidity=0.0, rh=40.0),
    )
    dry_only = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0, wind=_wx(humidity=0.0)
    )
    rh_40 = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0, wind=_wx(humidity=40.0)
    )
    assert both.components["ffmc_star"] == pytest.approx(dry_only.components["ffmc_star"])
    assert both.score == pytest.approx(dry_only.score)
    assert both.score > rh_40.score


def test_precip_zero_wins_over_precipitation_mm_key():
    """precip_mm=0.0 is a valid falsy reading — must not defer to fallback key."""
    zero_precip = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        wind={"kph": 15.0, "temperature_c": 25.0, "humidity": 40.0,
              "precip_mm": 0.0, "precipitation_mm": 10.0},
    )
    reference = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0, wind=_wx(precip_mm=0.0)
    )
    assert zero_precip.components["ffmc_star"] == pytest.approx(
        reference.components["ffmc_star"]
    )
    assert zero_precip.score == pytest.approx(reference.score)


# ---------------------------------------------------------------------------
# wind monotonicity
# ---------------------------------------------------------------------------

def test_isi_strictly_increases_with_wind():
    """Raw Van Wagner ISI is strictly monotone in wind for fixed FFMC."""
    for ffmc in (60.0, 80.0, 90.0, 95.0):
        winds = [0.0, 2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 80.0]
        isis = [_isi_from_ffmc_wind(ffmc, w) for w in winds]
        for lo, hi in zip(isis, isis[1:]):
            assert hi > lo, (ffmc, isis)


def test_score_never_decreases_as_wind_rises():
    """Below the ISI norm clamp the composite score is non-decreasing in wind."""
    scores = []
    for w in (0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0):
        out = compute_threat(
            _fire(), distance_km=5.0, within_km=50.0, wind=_wx(kph=w)
        )
        scores.append(out.score)
    for lo, hi in zip(scores, scores[1:]):
        assert hi >= lo - 1e-12, scores


# ---------------------------------------------------------------------------
# distance monotonicity
# ---------------------------------------------------------------------------

def test_score_non_increasing_as_distance_grows():
    scores = []
    for d in (0.0, 1.0, 5.0, 10.0, 20.0, 30.0, 40.0, 49.0, 50.0):
        out = compute_threat(_fire(), distance_km=d, within_km=50.0, wind=_wx())
        scores.append(out.score)
    for lo, hi in zip(scores, scores[1:]):
        assert hi <= lo + 1e-12, scores


def test_distance_at_within_km_zeroes_score():
    out = compute_threat(_fire(), distance_km=50.0, within_km=50.0, wind=_wx())
    assert out.components["proximity"] == pytest.approx(0.0)
    assert out.score == pytest.approx(0.0)


def test_distance_beyond_within_km_stays_zero():
    out = compute_threat(_fire(), distance_km=500.0, within_km=50.0, wind=_wx())
    assert out.score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# clamps under hostile input grids
# ---------------------------------------------------------------------------

_DISTANCES = [0.0, 5.0, -10.0, 1e12, float("nan"), float("inf"), None, "far"]
_WITHINS = [50.0, 0.0, -1.0, 1e-9, float("inf"), float("nan")]
_CONTAINMENTS = [-500.0, 0.0, 100.0, 1e6, float("nan"), "n/a", None]
_WINDS = [
    None,
    {},
    # 10,000 kph is absurd but stays under exp()'s overflow point (~14k
    # kph in the Van Wagner wind term); inf/NaN winds are rejected by
    # _safe_float upstream so they degrade instead of overflowing.
    _wx(kph=1e4, temperature_c=1e6, humidity=-1e6, precip_mm=-1e6),
    _wx(kph=float("inf"), temperature_c=float("nan")),
    {"kph": "gusty", "humidity": float("inf"), "precip_mm": object()},
]


def test_score_clamped_on_extreme_input_grid():
    """Every combination of hostile inputs stays finite in [0, 100]."""
    now = time.time()
    runaway = [(now - 3600.0, 0.0), (now, 1e12)]   # 1e12 ac/hr
    for d, w_km, cont, wind in itertools.product(
        _DISTANCES, _WITHINS, _CONTAINMENTS, _WINDS
    ):
        fire = _fire(containment=None)
        fire["data"]["containment"] = cont
        out = compute_threat(
            fire,
            distance_km=d,  # type: ignore[arg-type]
            within_km=w_km,
            acreage_history=runaway,
            wind=wind,
        )
        assert math.isfinite(out.score), (d, w_km, cont, wind)
        assert 0.0 <= out.score <= 100.0, (d, w_km, cont, wind, out.score)


def test_negative_and_over_100_containment_clamped():
    over = compute_threat(_fire(containment=150.0), distance_km=5.0, within_km=50.0)
    neg = compute_threat(_fire(containment=-50.0), distance_km=5.0, within_km=50.0)
    # containment > 100 → uncontained hits its 0.2 floor, not below.
    assert over.components["uncontained"] == pytest.approx(0.2)
    # containment < 0 → uncontained caps at 1.0, not above.
    assert neg.components["uncontained"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# planned-burn cap under extreme inputs
# ---------------------------------------------------------------------------

def test_planned_cap_holds_under_worst_case_inputs():
    now = time.time()
    fire = _fire(containment=-100.0, name="Massive Prescribed Burn", is_prescribed=True)
    out = compute_threat(
        fire,
        distance_km=0.0,
        within_km=50.0,
        acreage_history=[(now - 3600.0, 0.0), (now, 1e9)],
        wind=_wx(kph=200.0, temperature_c=50.0, humidity=0.0),
    )
    assert out.score <= 5.0
    assert any("capped" in n for n in out.explanation)


# ---------------------------------------------------------------------------
# growth edge cases
# ---------------------------------------------------------------------------

def test_single_sample_history_gives_growth_1x_with_note():
    out = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        acreage_history=[(time.time(), 1000.0)], wind=_wx(),
    )
    assert out.components["growth_mul"] == pytest.approx(1.0)
    assert any("growth" in n for n in out.explanation)


def test_sub_30min_window_gives_growth_1x_with_note():
    now = time.time()
    out = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        acreage_history=[(now - 29 * 60.0, 1000.0), (now, 5000.0)],
        wind=_wx(),
    )
    assert out.components["growth_mul"] == pytest.approx(1.0)
    assert any("growth" in n for n in out.explanation)


def test_shrinking_fire_growth_rate_is_zero():
    now = time.time()
    shrinking = [(now - 7200.0, 5000.0), (now, 1000.0)]
    assert _growth_rate_per_hour(shrinking) == pytest.approx(0.0)
    out = compute_threat(
        _fire(), distance_km=5.0, within_km=50.0,
        acreage_history=shrinking, wind=_wx(),
    )
    assert out.components["growth_rate"] == pytest.approx(0.0)
    assert out.components["growth_mul"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# unit parsing equivalence
# ---------------------------------------------------------------------------

def test_same_physical_wind_scores_equal_across_units():
    """36 kph expressed as kph / mph / mps / kt must score the same."""
    kph = 36.0
    variants = {
        "kph": {"kph": kph},
        "mph": {"mph": kph / 1.609344},
        "mps": {"mps": kph / 3.6},
        "kt": {"kt": kph / 1.852},
    }
    scores = {}
    for label, wind in variants.items():
        wind.update({"temperature_c": 25.0, "humidity": 40.0, "precip_mm": 0.0})
        out = compute_threat(_fire(), distance_km=5.0, within_km=50.0, wind=wind)
        assert out.components["wind_kph"] == pytest.approx(kph, rel=1e-9)
        scores[label] = out.score
    ref = scores["kph"]
    for label, score in scores.items():
        assert score == pytest.approx(ref, rel=1e-9), (label, scores)
