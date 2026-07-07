"""ANSI color palette and helpers for libwatchduty terminal output.

Provides palette constants, TTY-aware coloring, OSC 8 hyperlinks, and tree-drawing glyphs.
"""

from __future__ import annotations

import os
import sys
from typing import IO, Optional

# Reset / base style codes
RESET = "0"
BOLD = "1"
DIM = "2"

# Palette role codes (ANSI SGR parameter strings)
HEADING = "1;97"
DIM_ROLE = "0;90"
FIRE_ID = "1;96"
FIRE_NAME_ACTIVE = "1;31"
FIRE_NAME_INACTIVE = "0;90"
DISTANCE = "0;36"
ACREAGE = "0;33"
CONTAINMENT_GOOD = "0;32"
CONTAINMENT_BAD = "0;33"
ADDRESS = "0;90"
REPORTER = "1;35"
TIMESTAMP = "0;96"
UPDATE_TEXT = "0;97"
EVAC_WARN = "1;33"
EVAC_ORDER = "1;41;97"
ERROR = "1;31"
OK = "0;32"
TYPE_TAG = "0;94"
BRANCH = "0;90"

# Progress bar + chip + link roles
BAR_FG_GOOD = "0;32"
BAR_FG_MED = "0;33"
BAR_FG_BAD = "0;31"
BAR_BG = "0;90"
CHIP_AIRCRAFT = "1;46;30"
CHIP_RESOURCE = "1;43;30"
CHIP_LIVE = "1;42;30"
LINK = "4;36"

# Tree-drawing glyphs
TREE_BRANCH = "├─ "
TREE_LAST = "└─ "
TREE_PIPE = "│  "
TREE_BLANK = "   "

# Memoized isatty() results keyed by id(stream); use_color() is called per
# painted cell and isatty() is a syscall. Env vars are still read live.
_TTY_CACHE: dict = {}


def _reset_tty_cache() -> None:
    """Clear the memoized isatty() results (for tests)."""
    _TTY_CACHE.clear()


def use_color(stream: Optional[IO] = None) -> bool:
    """Return True if ANSI color should be emitted on ``stream``.

    Honors ``NO_COLOR`` (disable) and ``FORCE_COLOR`` (force on unless "0").
    Defaults to ``sys.stdout`` and otherwise requires the stream to be a TTY.
    """
    if stream is None:
        stream = sys.stdout

    force = os.environ.get("FORCE_COLOR")
    if force is not None and force != "0":
        return True
    if force == "0":
        return False
    if "NO_COLOR" in os.environ:
        return False
    key = id(stream)
    cached = _TTY_CACHE.get(key)
    if cached is None:
        try:
            cached = bool(stream.isatty())
        except (AttributeError, ValueError):
            cached = False
        _TTY_CACHE[key] = cached
    return cached


def paint(text: str, *codes: str, stream: Optional[IO] = None) -> str:
    """Wrap ``text`` in ANSI SGR ``codes`` if coloring is enabled.

    Multiple codes are joined with ';'. Returns ``text`` unchanged when no codes
    are given or when ``use_color(stream)`` is False.
    """
    if not codes:
        return text
    if not use_color(stream):
        return text
    joined = ";".join(codes)
    return f"\x1b[{joined}m{text}\x1b[0m"


def hyperlink(text: str, url: str, stream: Optional[IO] = None) -> str:
    """Render an OSC 8 terminal hyperlink, or fall back to ``"text (url)"``.

    Uses the escape sequence when ``use_color(stream)`` is True, otherwise
    returns a plain-text representation safe for logs and non-TTY sinks.
    """
    if not use_color(stream):
        return f"{text} ({url})"
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"
