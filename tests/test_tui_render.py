"""Pyte-driven smoke tests.

Skipped automatically when ``pyte`` isn't installed. The full TUI needs
real curses (and a tty), so instead we feed the visible-fire names into
a pyte screen and confirm we can pull them back out — proving the
pyte rendering path the TUI uses for embedded-mapscii also works for
arbitrary content.
"""

from __future__ import annotations

import pytest

pyte = pytest.importorskip("pyte")

from libwatchduty import tui as _tui


def _render_lines_into_pyte(lines: list[str], cols: int = 80, rows: int = 24) -> list[str]:
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    for i, ln in enumerate(lines[:rows]):
        # Move cursor to (i, 0), then write the line. Pyte expects bytes.
        stream.feed(f"\x1b[{i + 1};1H".encode("ascii"))
        stream.feed(ln.encode("utf-8", "replace"))
    return [row.rstrip() for row in screen.display]


def test_pyte_renders_visible_fire_names(tui_state):
    # Build a list of fire-name lines from the visible_fires set, then
    # render them through pyte and assert the cells round-trip.
    names = [str(f.get("name") or "") for f in tui_state.visible_fires]
    assert names, "fixture should have visible fires"
    out = _render_lines_into_pyte(names)
    rendered = "\n".join(out)
    for n in names:
        assert n in rendered, f"expected {n!r} in pyte output"


def test_pyte_handles_color_escapes_without_crash():
    screen = pyte.Screen(40, 5)
    stream = pyte.ByteStream(screen)
    # Color SGR + glyph — same kind of bytes the mapscii embed feeds.
    stream.feed(b"\x1b[31mHELLO\x1b[0m")
    # _pyte_color_index always returns an int regardless of fg/bg style.
    cell = screen.buffer[0][0]
    assert isinstance(_tui._pyte_color_index(cell.fg), int)
    # Text round-trips into the display.
    assert "HELLO" in screen.display[0]
