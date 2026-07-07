"""Typed shapes for Watch Duty API payloads.

These TypedDicts document the fields *observed* on the reverse-engineered
API — they are not a contract. Everything is ``total=False`` because the
server omits fields freely; treat every key as optional and keep using
``.get`` at access sites. Purely for annotations/IDE help; no runtime
validation happens anywhere.
"""

from __future__ import annotations

from typing import Any, TypedDict

__all__ = [
    "LatLng",
    "GeoEventData",
    "GeoEvent",
    "Report",
    "Camera",
    "RadioFeed",
]


class LatLng(TypedDict, total=False):
    lat: float
    lng: float


class GeoEventData(TypedDict, total=False):
    acreage: float
    containment: float
    is_prescribed: bool
    evacuation_orders: str
    evacuation_warnings: str
    evacuation_advisories: str
    links: list[dict]


class GeoEvent(TypedDict, total=False):
    id: int
    name: str
    geo_event_type: str
    is_active: bool
    lat: float
    lng: float
    address: str
    date_modified: str
    data: GeoEventData


class Report(TypedDict, total=False):
    id: int
    message: str
    date_created: str
    lat: float
    lng: float
    status: str
    user_created: dict
    media: list[dict[str, Any]]


class Camera(TypedDict, total=False):
    id: int
    name: str
    latlng: LatLng
    image_url: str
    is_offline: bool
    has_ptz: bool
    provider: str


class RadioFeed(TypedDict, total=False):
    feed_id: int
    name: str
    description: str
    online: bool
    listeners: int
    listen_url: str
