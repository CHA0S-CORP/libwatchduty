"""Inline image rendering for terminals that support graphics protocols.

Supports two protocols, auto-selected per stream:
- Kitty graphics protocol (kitty, ghostty, WezTerm with kitty mode)
- iTerm2 inline images protocol (iTerm.app, WezTerm with iterm mode, and
  the VS Code integrated terminal with ``terminal.integrated.enableImages``)

Detection requires the target stream to be a TTY. Returns ``None`` / empty
string on unsupported terminals so callers can fall back to OSC 8.

Auto-detection keys off environment variables the terminal sets. That
breaks when those vars don't reach the process — most notably inside a
container, where ``docker run`` does not forward the host's
``TERM_PROGRAM``. Set ``WATCHDUTY_INLINE_IMAGES`` to force a protocol
regardless of detection:

    WATCHDUTY_INLINE_IMAGES=iterm2   # VS Code terminal, iTerm2, WezTerm
    WATCHDUTY_INLINE_IMAGES=kitty    # kitty, ghostty
    WATCHDUTY_INLINE_IMAGES=off      # disable inline images entirely
    WATCHDUTY_INLINE_IMAGES=auto     # (default) sniff the environment

References:
- https://sw.kovidgoyal.net/kitty/graphics-protocol/
- https://iterm2.com/documentation-images.html
- https://code.visualstudio.com/docs/terminal/advanced#_image-support
"""

from __future__ import annotations

import base64
import os
import sys
from typing import IO, Any

KITTY_CHUNK = 4096  # base64 chars per payload chunk per the protocol


def _forced_protocol() -> str | None:
    """Return the protocol pinned via ``WATCHDUTY_INLINE_IMAGES``, if any.

    One of ``"iterm2"``, ``"kitty"``, ``"off"``, or ``None`` (auto-detect).
    Read fresh each call so the override responds to late env changes.
    """
    v = os.environ.get("WATCHDUTY_INLINE_IMAGES", "").strip().lower()
    if v in ("iterm2", "iterm", "iterm.app", "vscode"):
        return "iterm2"
    if v in ("kitty", "ghostty"):
        return "kitty"
    if v in ("off", "none", "no", "0", "false"):
        return "off"
    return None


def _isatty(stream: IO | None) -> bool:
    if stream is None:
        stream = sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def supports_kitty(stream: IO | None = None) -> bool:
    """Best-effort check that ``stream`` is a kitty (or ghostty) terminal.

    True iff stream isatty AND ``$TERM`` looks like kitty OR ``$TERM_PROGRAM``
    is ``"ghostty"`` (ghostty implements the protocol). ``WATCHDUTY_INLINE_IMAGES``
    overrides detection: ``kitty`` forces True, ``iterm2``/``off`` force False.
    False if ``NO_COLOR`` is set, because the user asked for plain output.
    """
    forced = _forced_protocol()
    if forced in ("off", "iterm2"):
        return False
    if not _isatty(stream):
        return False
    if forced == "kitty":
        return True
    if "NO_COLOR" in os.environ:
        return False
    term = os.environ.get("TERM", "")
    if "kitty" in term:
        return True
    if os.environ.get("TERM_PROGRAM") == "ghostty":
        return True
    if os.environ.get("KITTY_WINDOW_ID"):
        return True
    return False


def render_kitty(
    data: bytes,
    *,
    max_cols: int | None = None,
    max_rows: int | None = None,
    fmt: str = "100",
) -> str:
    """Build the escape sequence to draw ``data`` inline via kitty graphics.

    Args:
        data: raw image bytes (PNG/JPEG/etc).
        max_cols: clamp to N terminal cells wide.
        max_rows: clamp to N terminal cells tall.
        fmt: kitty format code; 100 = PNG per the protocol spec. Recent
            kitty (>= 0.28) and ghostty auto-detect JPEG data under f=100
            too; older strict implementations may reject non-PNG bytes.

    Returns the full escape sequence string. The caller is responsible for
    placing the cursor at the desired row first. Empty string if ``data``
    is empty.
    """
    if not data:
        return ""
    payload = base64.standard_b64encode(data).decode("ascii")
    # Build the header params for the first chunk.
    params = ["a=T", f"f={fmt}"]
    if max_cols:
        params.append(f"c={max_cols}")
    if max_rows:
        params.append(f"r={max_rows}")

    chunks: list[str] = []
    pos = 0
    first = True
    while pos < len(payload):
        chunk = payload[pos : pos + KITTY_CHUNK]
        pos += KITTY_CHUNK
        more = "1" if pos < len(payload) else "0"
        if first:
            head = ",".join(params + [f"m={more}"])
            first = False
        else:
            head = f"m={more}"
        chunks.append(f"\x1b_G{head};{chunk}\x1b\\")
    return "".join(chunks)


def supports_iterm2(stream: IO | None = None) -> bool:
    """Best-effort check that ``stream`` speaks the iTerm2 inline-image protocol.

    True iff stream isatty AND TERM_PROGRAM is 'iTerm.app' or 'vscode' (the
    VS Code integrated terminal renders iTerm2 escapes via its image addon),
    OR LC_TERMINAL == 'iTerm2' (what tmux preserves). ``WATCHDUTY_INLINE_IMAGES``
    overrides detection: ``iterm2`` forces True, ``kitty``/``off`` force False.
    False when NO_COLOR is set.
    """
    forced = _forced_protocol()
    if forced in ("off", "kitty"):
        return False
    if not _isatty(stream):
        return False
    if forced == "iterm2":
        return True
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "vscode"):
        return True
    if os.environ.get("LC_TERMINAL") == "iTerm2":
        return True
    return False


def render_iterm2(
    data: bytes,
    *,
    max_cols: int | None = None,
    max_rows: int | None = None,
    name: str = "img",
) -> str:
    """Build the escape sequence to draw ``data`` inline via the iTerm2 protocol.

    Args:
        data: raw image bytes (PNG/JPEG/etc).
        max_cols: clamp width to N character cells.
        max_rows: clamp height to N character cells.
        name: base64'd in the protocol header; cosmetic.
    """
    if not data:
        return ""
    payload = base64.standard_b64encode(data).decode("ascii")
    name_b64 = base64.standard_b64encode(name.encode("utf-8")).decode("ascii")
    args = [f"name={name_b64}", f"size={len(data)}", "inline=1", "preserveAspectRatio=1"]
    if max_cols:
        args.append(f"width={max_cols}")
    if max_rows:
        args.append(f"height={max_rows}")
    head = ";".join(args)
    return f"\x1b]1337;File={head}:{payload}\x07"


def supports_inline_images(stream: IO | None = None) -> bool:
    """True iff EITHER the kitty or iTerm2 protocol is supported on ``stream``."""
    return supports_kitty(stream) or supports_iterm2(stream)


def render_inline(
    data: bytes,
    *,
    max_cols: int | None = None,
    max_rows: int | None = None,
    stream: IO | None = None,
) -> str:
    """Render ``data`` using whichever protocol the terminal supports.

    Prefers iTerm2 on iTerm.app (kitty escapes don't render there); falls back
    to kitty otherwise. Returns an empty string if neither is supported.
    """
    if supports_iterm2(stream):
        return render_iterm2(data, max_cols=max_cols, max_rows=max_rows)
    if supports_kitty(stream):
        return render_kitty(data, max_cols=max_cols, max_rows=max_rows)
    return ""


def render_url(
    client: Any,
    url: str,
    *,
    max_cols: int | None = None,
    max_rows: int | None = None,
    stream: IO | None = None,
) -> str | None:
    """Fetch ``url`` and render inline via the best-supported protocol.

    Returns the escape string on success, or ``None`` when no protocol is
    supported OR the fetch fails. Caller should hyperlink-fallback on None.
    """
    if not supports_inline_images(stream):
        return None
    try:
        data = client.fetch_camera_image(url)
    except Exception:
        return None
    return render_inline(data, max_cols=max_cols, max_rows=max_rows, stream=stream)


__all__ = [
    "supports_kitty", "render_kitty",
    "supports_iterm2", "render_iterm2",
    "supports_inline_images", "render_inline",
    "render_url",
]
