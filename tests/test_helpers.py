"""Pure-function tests for TUI helpers."""

from __future__ import annotations

import pytest

from libwatchduty import tui as _tui


# ---------------------------------------------------------------------------
# bearing / distance
# ---------------------------------------------------------------------------

def test_initial_bearing_due_north_and_east():
    # Due-north (same lng, b is to the north of a) -> ~0°.
    assert _tui._initial_bearing((0.0, 0.0), (1.0, 0.0)) == pytest.approx(0.0, abs=0.1)
    # Due-east at the equator -> ~90°.
    assert _tui._initial_bearing((0.0, 0.0), (0.0, 1.0)) == pytest.approx(90.0, abs=0.1)


def test_bearing_arrow_eight_compass_points():
    # Centre of each 45° wedge -> the canonical arrow for that wedge.
    expected = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
    got = [_tui._bearing_arrow(d) for d in (0, 45, 90, 135, 180, 225, 270, 315)]
    assert got == expected
    # None -> a dot, never an index error.
    assert _tui._bearing_arrow(None) == "·"


def test_haversine_km_symmetry_and_zero():
    a, b = (34.0, -117.0), (34.5, -117.5)
    d1 = _tui._haversine_km(a, b)
    d2 = _tui._haversine_km(b, a)
    assert d1 == pytest.approx(d2)
    assert _tui._haversine_km(a, a) == pytest.approx(0.0, abs=1e-9)
    # Sanity range — half a degree at 34°N is ~60 km.
    assert 60.0 < d1 < 80.0


# ---------------------------------------------------------------------------
# sparkline
# ---------------------------------------------------------------------------

def test_sparkline_ramp_monotonic():
    s = _tui._sparkline([1.0, 2.0, 3.0, 4.0], width=4)
    assert len(s) == 4
    # ascending input -> chars rank-ascending in the ramp.
    ranks = [_tui._SPARK_RAMP.index(c) for c in s]
    assert ranks == sorted(ranks)


def test_sparkline_edges():
    assert _tui._sparkline([], width=4) == ""
    assert _tui._sparkline([5.0], width=0) == ""
    # all-equal collapses to the mid-ramp glyph repeated.
    flat = _tui._sparkline([2.0, 2.0, 2.0], width=3)
    assert flat == _tui._SPARK_RAMP[3] * 3


# ---------------------------------------------------------------------------
# threat scoring
# ---------------------------------------------------------------------------

def test_threat_factors_planned_floor():
    fire = {"id": 1, "lat": 34.0, "lng": -117.0,
            "data": {"acreage": 1000.0, "containment": 0.0, "is_prescribed": True}}
    f = _tui._threat_factors(
        fire, distance_km=5.0, within_km=250.0,
        acreage_hist=[], wind=None, near=(34.0, -117.0),
    )
    assert 0.0 <= f["score"] <= 5.0
    assert f["planned"] == 1.0


def test_threat_factors_clamped_range():
    fire = {"id": 2, "lat": 34.0, "lng": -117.0,
            "data": {"acreage": 50000.0, "containment": 0.0}}
    f = _tui._threat_factors(
        fire, distance_km=0.0, within_km=250.0,
        acreage_hist=[(0.0, 100.0), (1.0, 1000.0)],   # 10x growth
        wind={"speed": 30.0, "bearing": 0.0}, near=(34.0, -117.0),
    )
    assert 0.0 <= f["score"] <= 100.0
    assert f["growth"] > 1.0   # growth multiplier kicked in


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def test_split_html_lines_paragraphs_and_br():
    s = "<p>line one</p><p>line two<br>line three</p>"
    assert _tui._split_html_lines(s) == ["line one", "line two", "line three"]
    assert _tui._split_html_lines("") == []
    assert _tui._split_html_lines(None) == []  # type: ignore[arg-type]


def test_wrap_around_image_narrow_then_wide():
    text = " ".join(["word"] * 12)
    out = _tui._wrap_around_image(text, narrow_w=10, wide_w=40, narrow_rows=2)
    # at least the first narrow_rows lines must fit narrow_w.
    for line in out[:2]:
        assert len(line) <= 10
    # empty input still yields a single empty line.
    assert _tui._wrap_around_image("", 10, 40, 2) == [""]


# ---------------------------------------------------------------------------
# mercator + pyte color
# ---------------------------------------------------------------------------

def test_mercator_pixel_origin_and_center():
    # zoom=0 → world is 256 px square. (0,0) lands at center.
    px, py = _tui._mercator_pixel(0.0, 0.0, 0)
    assert px == pytest.approx(128.0)
    assert py == pytest.approx(128.0)
    # +1 zoom doubles the world.
    px1, py1 = _tui._mercator_pixel(0.0, 0.0, 1)
    assert px1 == pytest.approx(256.0)
    assert py1 == pytest.approx(256.0)


def test_pyte_color_index_named_hex_default():
    assert _tui._pyte_color_index("default") == -1
    assert _tui._pyte_color_index(None) == -1
    assert _tui._pyte_color_index("red") == 1
    # hex maps into the 6x6x6 color cube (16..231).
    idx = _tui._pyte_color_index("ff0000")
    assert 16 <= idx <= 231
    # integers clamp to [-1, 255].
    assert _tui._pyte_color_index(300) == 255
    assert _tui._pyte_color_index(-5) == -1
