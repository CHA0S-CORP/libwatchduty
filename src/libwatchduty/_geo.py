"""Shared geographic math for libwatchduty.

Single home for the haversine + bearing helpers that were previously
duplicated across ``cli``, ``tui``, and ``stills``. ``threat`` keeps its
own private copy on purpose — that module is deliberately zero-import so
it stays trivially testable in isolation.
"""

from __future__ import annotations

from math import asin, atan2, cos, degrees, radians, sin, sqrt

# Mean Earth radius (IUGG) in km.
EARTH_R_KM = 6371.0088

__all__ = ["EARTH_R_KM", "haversine_km", "initial_bearing"]


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two ``(lat, lng)`` points in km.

    Inputs are decimal degrees; the result is a non-negative float.
    """
    lat1, lng1 = a
    lat2, lng2 = b
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    h = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    )
    return 2 * EARTH_R_KM * asin(sqrt(h))


def initial_bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial great-circle bearing from ``a`` → ``b``, degrees in [0, 360)."""
    la1 = radians(a[0])
    la2 = radians(b[0])
    dlng = radians(b[1] - a[1])
    y = sin(dlng) * cos(la2)
    x = cos(la1) * sin(la2) - sin(la1) * cos(la2) * cos(dlng)
    return (degrees(atan2(y, x)) + 360.0) % 360.0
