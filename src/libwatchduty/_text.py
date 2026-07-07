"""Shared text/HTML/timestamp helpers for libwatchduty.

Single home for the strip-HTML and relative-age helpers previously
duplicated between ``cli`` and ``tui``. Not a general-purpose HTML
sanitiser — targets the small dialect Watch Duty reporters actually emit.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

__all__ = [
    "strip_html",
    "split_html_lines",
    "seconds_since_iso",
    "format_age",
    "format_age_relative",
]

_BR_RE = re.compile(r"<br\s*/?>", re.I)
_PARA_RE = re.compile(r"</p>\s*<p>", re.I)
_BLOCK_RE = re.compile(r"</?(?:p|li|ul|ol|div|h[1-6])[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str, para_sep: str = " | ") -> str:
    """Reduce an HTML ``message`` body to a single flat line.

    Converts ``<br>`` to a space, paragraph breaks to ``para_sep``, drops
    all other tags, decodes entities, and collapses whitespace runs.
    """
    if not s:
        return ""
    s = _BR_RE.sub(" ", s)
    s = _PARA_RE.sub(para_sep, s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    return " ".join(s.split())


def split_html_lines(s: str) -> list[str]:
    """Like :func:`strip_html` but keeps paragraph / list / br breaks as
    separate non-empty lines so callers can render each on its own row.
    """
    if not s:
        return []
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    out: list[str] = []
    for part in s.split("\n"):
        cleaned = " ".join(part.split())
        if cleaned:
            out.append(cleaned)
    return out


def _parse_iso(iso: str) -> datetime | None:
    """Best-effort ISO-8601 parse; assumes UTC when no tz is present."""
    s = iso.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def seconds_since_iso(iso: str) -> float:
    """Seconds since an ISO-8601 timestamp; 0.0 on parse error."""
    if not iso:
        return 0.0
    try:
        dt = _parse_iso(iso)
    except Exception:
        return 0.0
    if dt is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def format_age(seconds: float) -> str:
    """Compact relative duration (`12s`, `3m`, `2h`, `4d`)."""
    if seconds < 0:
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def format_age_relative(iso: str | None) -> str:
    """Compact relative-age string like ``"3h ago"``.

    Accepts ISO-8601 with optional trailing Z. Returns ``""`` on parse
    error or when the timestamp is in the future by more than a few
    seconds.
    """
    if not iso:
        return ""
    try:
        dt = _parse_iso(iso)
    except Exception:
        return ""
    if dt is None:
        return ""
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < -5:
        return ""
    s_ = max(0, int(delta))
    if s_ < 60:
        return f"{s_}s ago"
    if s_ < 3600:
        return f"{s_ // 60}m ago"
    if s_ < 86400:
        return f"{s_ // 3600}h ago"
    if s_ < 86400 * 30:
        return f"{s_ // 86400}d ago"
    return f"{s_ // 2592000}mo ago"
