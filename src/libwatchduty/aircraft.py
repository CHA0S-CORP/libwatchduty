"""Aircraft catalog cache + regex chip extraction from report text.

The Watch Duty API exposes ``/aircraft/`` as a global catalog only — there is
no per-fire assignment endpoint. This module:

1. Caches the catalog on disk (~/.cache/libwatchduty/aircraft.json, 24h TTL).
2. Lets callers look up an aircraft by tail/callsign/hex/etc.
3. Extracts plausible aircraft + resource mentions from free-form update text
   so the TUI can render them as colored chips. Conservative: prefers
   precision over recall.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_PATH = Path(
    os.environ.get("XDG_CACHE_HOME")
    or Path.home() / ".cache"
) / "libwatchduty" / "aircraft.json"


def _cache_path() -> Path:
    """Resolve the on-disk cache path, creating parents lazily on write."""
    return CACHE_PATH


def load_catalog(client: Any, *, force_refresh: bool = False) -> list[dict]:
    """Return the aircraft catalog, served from cache when fresh.

    Args:
        client: any WatchDutyClient (uses ``client.list_aircraft()``).
        force_refresh: bypass the on-disk cache and refetch.

    Cache TTL is 24h. On any cache read error the catalog is refetched.
    """
    p = _cache_path()
    if not force_refresh and p.is_file():
        try:
            age = time.time() - p.stat().st_mtime
            if age < CACHE_TTL_SECONDS:
                return json.loads(p.read_text())
        except (OSError, ValueError):
            pass
    catalog = client.list_aircraft() or []
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(catalog))
    except OSError:
        pass
    return catalog


_SEARCH_FIELDS = ("tail_num", "hex_code", "short_callsign", "name", "model", "type")


def lookup(catalog: list[dict], query: str, *, limit: int = 50) -> list[dict]:
    """Case-insensitive substring match across the common aircraft fields.

    Returns up to ``limit`` matches preserving catalog order.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    out: list[dict] = []
    for a in catalog:
        for f in _SEARCH_FIELDS:
            v = a.get(f)
            if isinstance(v, str) and q in v.lower():
                out.append(a)
                break
        if len(out) >= limit:
            break
    return out


# Chip-extraction patterns. Order matters — more specific first so a generic
# "tanker" mention does not eat an exact "T-105" match.
_CHIP_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("aircraft", re.compile(r"\bT-?\d{1,4}\b", re.I), "tanker"),
    ("aircraft", re.compile(r"\b(?:Air[\s-]?Attack|AA)[\s-]?\d{1,4}\b", re.I), "air_attack"),
    ("aircraft", re.compile(r"\b(?:Helo|Helicopter|H)[\s-]?\d{1,4}\b", re.I), "helo"),
    # Require >=3 digits or a letter suffix, and no leading zero, so short
    # non-registration tokens like "N95" (respirator masks) don't match.
    ("aircraft", re.compile(r"\bN(?:[1-9]\d{2,4}[A-Z]{0,2}|[1-9]\d{0,3}[A-Z]{1,2})\b"), "tail"),
    ("aircraft", re.compile(r"\bC-?130\b", re.I), "c130"),
    ("aircraft", re.compile(r"\bMD-?87\b", re.I), "md87"),
    ("aircraft", re.compile(r"\bDC-?10\b", re.I), "dc10"),
    ("resource", re.compile(r"\btype\s*[1-7]\s*(?:air\s*tankers?|helicopters?|engines?|crews?|strike\s*teams?)\b", re.I), "resource"),
    ("resource", re.compile(r"\b(?:VLAT|LAT|SEAT|MAFFS|Hotshots?|Smokejumpers?|Air\s*Attack|Helitack)\b", re.I), "resource"),
    ("resource", re.compile(r"\b\d{1,3}\s*(?:engines?|crews?|hand\s*crews?|dozers?|water\s*tenders?)\b", re.I), "resource"),
]


def extract_chips(text: str) -> list[dict]:
    """Find plausible aircraft + resource mentions in ``text``.

    Returns a list of ``{label, kind, matched_text, pattern}`` dicts in source
    order, de-duplicated by ``label.lower()``. ``label`` is the matched text
    trimmed of trailing punctuation; ``kind`` is "aircraft" or "resource".

    Conservative — fragile field; tweak ``_CHIP_PATTERNS`` to add coverage.
    """
    if not text:
        return []
    seen: set[str] = set()
    chips: list[dict] = []
    for kind, pat, name in _CHIP_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(0).strip(" .,;:!?()")
            key = raw.lower()
            if key in seen:
                continue
            seen.add(key)
            chips.append({
                "label": raw,
                "kind": kind,
                "matched_text": raw,
                "pattern": name,
            })
    return chips


def enrich_chips(
    chips: list[dict], catalog: list[dict]
) -> list[dict]:
    """Attach ``catalog_hit`` to each chip when its label matches a catalog row.

    Returns a new list; original chips are not mutated.
    """
    if not chips:
        return []
    out: list[dict] = []
    for chip in chips:
        hit = None
        if chip["kind"] == "aircraft":
            hits = lookup(catalog, chip["label"], limit=1)
            if hits:
                hit = hits[0]
        out.append({**chip, "catalog_hit": hit})
    return out


__all__ = ["load_catalog", "lookup", "extract_chips", "enrich_chips", "CACHE_TTL_SECONDS"]
