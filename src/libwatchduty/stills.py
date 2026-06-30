"""Download still frames from Watch Duty wildfire-detection cameras.

Image URLs returned by /cameras_gis/realtime point at third-party hosts
(alertwest.com, alertcalifornia.org, etc). This module GETs them through
the configured WatchDutyClient session, writes them to disk with a
deterministic name, and supports a simple timelapse capture loop.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_IMAGE_FIELD = "image_url"
DEFAULT_ID_FIELD = "id"
DEFAULT_NAME_FIELD = "name"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str, max_len: int = 40) -> str:
    """Lowercase, alnum-or-dash slug; clipped to max_len."""
    s = _SLUG_RE.sub("-", (s or "").lower()).strip("-")
    return s[:max_len] or "unnamed"


def still_filename(
    camera: dict,
    *,
    ts: datetime | None = None,
    ext: str = "jpg",
    id_field: str = DEFAULT_ID_FIELD,
    name_field: str = DEFAULT_NAME_FIELD,
) -> str:
    """Build a filename like ``20260630T005153Z__<id>__<slug>.jpg``.

    Args:
        camera: realtime-camera dict (must contain id_field and name_field).
        ts: timestamp, defaults to now in UTC.
        ext: file extension, no leading dot.
        id_field/name_field: where to read id and name from the camera dict.
    """
    when = (ts or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    cid = str(camera.get(id_field, "unknown"))
    slug = _slugify(camera.get(name_field) or "")
    return f"{when}__{cid}__{slug}.{ext}"


def save_still(
    client: Any,
    url: str,
    out_path: str | os.PathLike,
    *,
    timeout: float | None = None,
) -> int:
    """Download a single still URL via the client's session. Returns bytes written.

    Creates parent directories as needed. Raises WatchDutyError on non-2xx.
    """
    data = client.fetch_camera_image(url, timeout=timeout)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return len(data)


def _filter_cameras(
    cameras: list[dict],
    *,
    camera_ids: Iterable[str | int] | None,
    lat: float | None,
    lng: float | None,
    radius_km: float | None,
    id_field: str,
) -> list[dict]:
    """Filter realtime cameras by explicit ids, or by proximity to lat/lng."""
    if camera_ids:
        wanted = {str(x) for x in camera_ids}
        return [c for c in cameras if str(c.get(id_field)) in wanted]
    if lat is None or lng is None:
        return cameras
    from math import asin, cos, radians, sin, sqrt

    def km(c: dict) -> float:
        ll = c.get("latlng") or {}
        a, b = ll.get("lat"), ll.get("lng")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return float("inf")
        dlat = radians(a - lat)
        dlng = radians(b - lng)
        h = sin(dlat / 2) ** 2 + cos(radians(lat)) * cos(radians(a)) * sin(dlng / 2) ** 2
        return 2 * 6371.0088 * asin(sqrt(h))

    with_dist = sorted(((km(c), c) for c in cameras), key=lambda x: x[0])
    if radius_km is None:
        return [c for _, c in with_dist if _ != float("inf")]
    return [c for d, c in with_dist if d <= radius_km]


def capture_cameras(
    client: Any,
    *,
    out_dir: str | os.PathLike,
    camera_ids: Iterable[str | int] | None = None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
    limit: int | None = None,
    image_field: str = DEFAULT_IMAGE_FIELD,
    id_field: str = DEFAULT_ID_FIELD,
    name_field: str = DEFAULT_NAME_FIELD,
    quiet: bool = False,
) -> list[str]:
    """Capture one still per matching camera into ``out_dir/<UTCstamp>/``.

    Filter precedence: ``camera_ids`` > ``lat/lng[/radius_km]`` > all.
    Per-camera failures are logged to stderr but do not abort the batch.
    Returns the list of file paths actually written.
    """
    cams = client.list_cameras() or []
    cams = _filter_cameras(
        cams,
        camera_ids=camera_ids,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        id_field=id_field,
    )
    if limit is not None:
        cams = cams[:limit]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(out_dir) / stamp
    written: list[str] = []
    for cam in cams:
        url = cam.get(image_field)
        if not url:
            continue
        try:
            fname = still_filename(cam, id_field=id_field, name_field=name_field)
            path = base / fname
            save_still(client, url, path)
            written.append(str(path))
        except Exception as e:
            if not quiet:
                print(
                    f"stills: {cam.get(id_field, '?')} failed: {e}",
                    file=sys.stderr,
                )
    return written


def capture_loop(
    client: Any,
    *,
    out_dir: str | os.PathLike,
    interval: float = 60.0,
    duration: float | None = None,
    **kwargs: Any,
) -> Iterator[list[str]]:
    """Yield the list of written paths each round; loop until duration elapses.

    Extra kwargs are forwarded to ``capture_cameras``. Pass ``duration=None`` to
    loop forever. Suspend via KeyboardInterrupt — generator exits cleanly.
    """
    if interval <= 0:
        raise ValueError("interval must be > 0")
    deadline = (time.monotonic() + duration) if duration is not None else None
    while True:
        yield capture_cameras(client, out_dir=out_dir, **kwargs)
        if deadline is not None and time.monotonic() >= deadline:
            return
        if deadline is not None:
            wait = min(interval, deadline - time.monotonic())
            if wait <= 0:
                return
            time.sleep(wait)
        else:
            time.sleep(interval)
