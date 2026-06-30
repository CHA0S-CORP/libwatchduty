"""Auto-detect approximate caller location for watchduty CLI defaults.

Privacy note
------------
This module exists to spare the user from typing coordinates every invocation.
It is *not* private:

* The IP-geolocation fallbacks send your public IP address to a third-party
  HTTPS endpoint (``ipapi.co`` first, ``ipwho.is`` as backup) so the remote
  service can map IP -> city -> approximate lat/lng. Those services see your
  IP and the bare fact that a ``watchduty-cli/auto-locate`` client asked for
  a lookup; nothing else identifying is sent.
* On macOS, if ``CoreLocationCLI`` is installed (``brew install
  corelocationcli``) we shell out to it. The first call triggers a one-time
  Location Services consent prompt under
  ``System Settings > Privacy & Security > Location Services`` for whichever
  terminal app spawned us; later calls are silent. We never persist the
  result.

Pass ``--near LAT,LNG`` (or a city name) on the CLI to bypass this module
entirely and avoid both side-effects.

The public entry point is :func:`detect_location`, which never raises and
returns ``None`` if every source fails inside the time budget.
"""

from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional

_USER_AGENT = "watchduty-cli/auto-locate"
_MAX_BODY_BYTES = 8 * 1024  # cap each response at 8 KiB
_MIN_USABLE_BUDGET = 0.05  # below this, skip the step rather than fire a doomed call
_CORELOC_CAP = 1.2  # per-step cap for the macOS shell-out
_HTTP_CAP = 1.5  # per-step cap for each HTTP fallback


def detect_location(
    timeout: float = 2.0,
) -> Optional[tuple[float, float, str]]:
    """Best-effort geolocate the caller within ``timeout`` wall-clock seconds.

    Tries, in order, until one succeeds: macOS CoreLocationCLI (if installed),
    then ``https://ipapi.co/json/``, then ``https://ipwho.is/``. Every error
    -- network, parse, permission, timeout -- is swallowed; only a usable
    ``(lat, lng, source_label)`` triple or ``None`` is ever returned. The
    function never raises.

    Parameters
    ----------
    timeout:
        Total wall-clock budget in seconds for the whole fallback chain.
        Each step gets ``min(per_step_cap, remaining_budget)`` and is skipped
        once the remaining budget drops below ~50 ms.

    Returns
    -------
    tuple[float, float, str] | None
        ``(latitude, longitude, source_label)`` where ``source_label`` is a
        short stable string like ``'corelocation'``, ``'ip:ipapi.co'``, or
        ``'ip:ipwho.is'``. Returns ``None`` if every source failed or the
        budget was exhausted.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))

    if platform.system() == "Darwin" and shutil.which("CoreLocationCLI"):
        budget = _remaining(deadline, _CORELOC_CAP)
        if budget >= _MIN_USABLE_BUDGET:
            result = _try_corelocation(budget)
            if result is not None:
                return result

    for host in ("ipapi.co", "ipwho.is"):
        budget = _remaining(deadline, _HTTP_CAP)
        if budget < _MIN_USABLE_BUDGET:
            break
        if host == "ipapi.co":
            result = _try_ipapi_co(budget)
        else:
            result = _try_ipwho_is(budget)
        if result is not None:
            return result

    return None


def _remaining(deadline: float, per_step_cap: float) -> float:
    """Return per-step timeout clamped to whatever is left on the deadline."""
    left = deadline - time.monotonic()
    if left <= 0:
        return 0.0
    return min(per_step_cap, left)


def _valid_latlng(lat: object, lng: object) -> Optional[tuple[float, float]]:
    """Coerce ``lat``/``lng`` to floats and bounds-check them, else None."""
    try:
        flat = float(lat)  # type: ignore[arg-type]
        flng = float(lng)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if flat != flat or flng != flng:  # NaN check without importing math
        return None
    if not (-90.0 <= flat <= 90.0):
        return None
    if not (-180.0 <= flng <= 180.0):
        return None
    return flat, flng


def _try_corelocation(timeout: float) -> Optional[tuple[float, float, str]]:
    """Shell out to CoreLocationCLI on macOS; parse lat/lng from stdout.

    Different builds of CoreLocationCLI emit different default formats:
    some honour ``-format %latitude,%longitude``, some print the bare
    ``"<lat> <lng>"`` pair separated by whitespace, some include city
    and country fields. We try both invocations and accept any leading
    two-float pair we can extract.
    """
    invocations = (
        ["CoreLocationCLI", "-once", "YES", "-format", "%latitude,%longitude"],
        ["CoreLocationCLI", "-once", "YES"],
    )
    for argv in invocations:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            continue
        first = (proc.stdout or "").strip().splitlines()[:1]
        if not first:
            continue
        # Accept comma OR whitespace separators (and tolerate trailing fields).
        raw = first[0].replace(",", " ").split()
        if len(raw) < 2:
            continue
        coords = _valid_latlng(raw[0], raw[1])
        if coords is None:
            continue
        return coords[0], coords[1], "corelocation"
    return None


def _http_get_json(url: str, timeout: float) -> Optional[dict]:
    """GET a JSON document over HTTPS with our User-Agent; return dict or None."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
            raw = resp.read(_MAX_BODY_BYTES + 1)
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError):
        return None
    if not raw or len(raw) > _MAX_BODY_BYTES:
        # Empty body or suspiciously large -- treat as failure rather than
        # blindly parse a truncated payload.
        if not raw or len(raw) > _MAX_BODY_BYTES:
            try:
                # If it's just barely over, still try the first 8 KiB; many
                # endpoints emit small bodies, so the >cap case is unusual.
                raw = raw[:_MAX_BODY_BYTES]
            except Exception:
                return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _try_ipapi_co(timeout: float) -> Optional[tuple[float, float, str]]:
    """Hit https://ipapi.co/json/ and extract numeric latitude/longitude."""
    data = _http_get_json("https://ipapi.co/json/", timeout)
    if data is None:
        return None
    if data.get("error"):  # documented rate-limit / error shape
        return None
    coords = _valid_latlng(data.get("latitude"), data.get("longitude"))
    if coords is None:
        return None
    return coords[0], coords[1], "ip:ipapi.co"


def _try_ipwho_is(timeout: float) -> Optional[tuple[float, float, str]]:
    """Hit https://ipwho.is/ and extract numeric latitude/longitude on success."""
    data = _http_get_json("https://ipwho.is/", timeout)
    if data is None:
        return None
    if data.get("success") is not True:
        return None
    coords = _valid_latlng(data.get("latitude"), data.get("longitude"))
    if coords is None:
        return None
    return coords[0], coords[1], "ip:ipwho.is"
