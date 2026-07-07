"""Embedded + fullscreen mapscii: PTY host, pyte painter, binary discovery."""

from __future__ import annotations

import curses
import errno
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from math import log, pi, radians, sin

from .layout import _addnstr
from .palette import _embed_pair_attr, _pyte_color_index


# ---------------------------------------------------------------------------
# Embedded mapscii (PTY hosted, paints into the Map tab rectangle)
# ---------------------------------------------------------------------------

_ANSI_CUP_RE = re.compile(rb"\x1b\[(\d*);?(\d*)H")
_ANSI_HVP_RE = re.compile(rb"\x1b\[(\d*);?(\d*)f")
_ANSI_FULL_CLEAR_RE = re.compile(rb"\x1b\[2J")
_ANSI_HOME_RE = re.compile(rb"\x1b\[H")


_MAPSCII_FOOTER_RE = re.compile(
    r"center:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"
    r".*?zoom:\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _mercator_pixel(lat: float, lng: float, zoom: float) -> tuple[float, float]:
    """Web-Mercator world pixel for (lat, lng) at the given fractional zoom.

    Mapscii draws tiles at 256 px per tile-edge, so this returns pixels in
    the same coordinate system mapscii uses internally — letting us compute
    the cell offset between the map's current centre and the fire point.
    """
    n = 2.0 ** float(zoom)
    world_px = 256.0 * n
    px = (float(lng) + 180.0) / 360.0 * world_px
    sl = max(-0.9999, min(0.9999, sin(radians(float(lat)))))
    py = (0.5 - log((1.0 + sl) / (1.0 - sl)) / (4.0 * pi)) * world_px
    return px, py


class _MapsciiEmbed:
    """Hosts a mapscii process in a background PTY and emulates a VT
    inside the Map-tab rectangle using `pyte`. Each tick we drain the
    PTY into pyte's virtual screen, then paint the visible cells into
    curses so the result composes correctly with the rest of the
    dashboard (no flicker, no escape-rewriting fragility).
    """

    def __init__(self, binary: str, lat: float, lng: float, zoom: int,
                 rows: int, cols: int):
        self.binary = binary
        self.fire_lat = float(lat)
        self.fire_lng = float(lng)
        self.fire_key: tuple[float, float, int] = (
            round(lat, 5), round(lng, 5), int(zoom),
        )
        self.rows = max(8, rows)
        self.cols = max(20, cols)
        self.pid = -1
        self.fd = -1
        self.alive = False
        self.screen = None
        self.stream = None
        try:
            import pyte  # type: ignore
        except ImportError:
            self.unavailable = (
                "embedded mapscii needs `pyte` — "
                "install with `pip install libwatchduty[tui]`"
            )
            return
        self.unavailable = None
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.ByteStream(self.screen)
        self._spawn(lat, lng, zoom)

    def _spawn(self, lat: float, lng: float, zoom: int) -> None:
        try:
            pid, fd = pty.fork()
        except OSError:
            return
        if pid == 0:
            # child
            os.environ["TERM"] = "xterm-256color"
            os.environ["LINES"] = str(self.rows)
            os.environ["COLUMNS"] = str(self.cols)
            os.environ["MAPSCII_LAT"] = f"{lat:.5f}"
            os.environ["MAPSCII_LNG"] = f"{lng:.5f}"
            os.environ["MAPSCII_ZOOM"] = str(int(zoom))
            try:
                os.execvp(self.binary, [self.binary])
            except OSError:
                os._exit(127)
        self.pid = pid
        self.fd = fd
        self.alive = True
        try:
            ws = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, ws)
            fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        except OSError:
            pass

    def matches(self, lat: float, lng: float, zoom: int) -> bool:
        return (self.fire_key == (round(lat, 5), round(lng, 5), int(zoom))
                and self.alive)

    def resize(self, rows: int, cols: int) -> None:
        rows = max(8, rows)
        cols = max(20, cols)
        if (rows, cols) == (self.rows, self.cols):
            return
        self.rows, self.cols = rows, cols
        if self.screen:
            try:
                # pyte preserves the old cell contents on resize, which
                # leaves a ghost of the previous frame in the new
                # rectangle until mapscii rerenders. Reset clears the
                # buffer so we paint whitespace until the next frame
                # arrives — far less jarring than seeing scrambled tiles.
                self.screen.resize(rows, cols)
                self.screen.reset()
                self.screen.resize(rows, cols)
            except Exception:
                pass
        try:
            ws = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, ws)
            os.kill(self.pid, signal.SIGWINCH)
            # Nudge mapscii's render pipeline — its SIGWINCH handler
            # re-draws, but sending a no-op cursor key prompts another
            # frame so the new size catches even if SIGWINCH is debounced
            # internally.
            os.write(self.fd, b"\x1b[C\x1b[D")
        except OSError:
            pass

    def poll(self) -> bool:
        """Feed any new PTY bytes into pyte's screen."""
        if not self.alive or not self.stream:
            return False
        got = False
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                self.alive = False
                return got
            if not data:
                self.alive = False
                break
            try:
                self.stream.feed(data)
            except Exception:
                pass
            got = True
        return got

    def send(self, data: bytes) -> None:
        """Write keystrokes to mapscii (arrow keys, a/z, q etc.)."""
        if not self.alive:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def _current_center(self) -> tuple[float, float, float] | None:
        """Parse mapscii's footer for ``(lat, lng, zoom)``. Returns None
        when the footer isn't on screen yet (e.g. tiles still loading)."""
        if not self.screen:
            return None
        try:
            display = self.screen.display
        except Exception:
            return None
        # Footer is the last row but mapscii also writes a notification
        # on the first row — scan from the bottom up for the first match.
        for sy in range(len(display) - 1, -1, -1):
            row = display[sy]
            m = _MAPSCII_FOOTER_RE.search(row)
            if m:
                try:
                    return float(m.group(1)), float(m.group(2)), float(m.group(3))
                except ValueError:
                    return None
        return None

    def _fire_cell(self) -> tuple[int, int] | None:
        """Where in our (cols, rows) cell grid the fire should be drawn,
        or None if it lies outside the visible viewport."""
        c = self._current_center()
        if c is None:
            # Footer not parsed yet → assume mapscii is still on the
            # initial frame for `(fire_lat, fire_lng)`; centre is the
            # fire itself.
            return self.cols // 2, self.rows // 2
        clat, clng, czoom = c
        fpx, fpy = _mercator_pixel(self.fire_lat, self.fire_lng, czoom)
        cpx, cpy = _mercator_pixel(clat, clng, czoom)
        # Braille glyph = 2 horizontal × 4 vertical pixels per cell.
        dx_cell = int(round((fpx - cpx) / 2.0))
        dy_cell = int(round((fpy - cpy) / 4.0))
        sx = self.cols // 2 + dx_cell
        sy = self.rows // 2 + dy_cell
        # Leave the footer row alone.
        if 0 <= sx < self.cols and 0 <= sy < max(1, self.rows - 1):
            return sx, sy
        return None

    def paint(self, stdscr, y0: int, x0: int, holder: dict) -> None:
        """Paint pyte's virtual screen into curses cells at (y0, x0),
        then overlay a fire marker at the projected cell."""
        if not self.screen:
            return
        try:
            buf = self.screen.buffer
        except Exception:
            return
        for sy in range(self.rows):
            row = buf[sy]
            for sx in range(self.cols):
                cell = row[sx]
                ch = cell.data or " "
                if not ch:
                    ch = " "
                fg = _pyte_color_index(cell.fg)
                bg = _pyte_color_index(cell.bg)
                attr = _embed_pair_attr(fg, bg)
                if cell.bold:
                    attr |= curses.A_BOLD
                if cell.reverse:
                    attr |= curses.A_REVERSE
                _addnstr(stdscr, y0 + sy, x0 + sx, ch[:1], 1, attr)
        # Fire marker on top.
        fc = self._fire_cell()
        if fc is not None:
            sx, sy = fc
            marker_attr = (
                _embed_pair_attr(_pyte_color_index("red"), -1)
                | curses.A_BOLD | curses.A_REVERSE
            )
            _addnstr(stdscr, y0 + sy, x0 + sx, "▲", 1, marker_attr)

    def close(self) -> None:
        if self.pid > 0:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
            # Reap for up to ~0.5s; SIGKILL + blocking wait if it lingers —
            # a lone WNOHANG right after SIGTERM almost always misses and
            # leaves a zombie until interpreter exit.
            reaped = False
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                try:
                    pid, _ = os.waitpid(self.pid, os.WNOHANG)
                except OSError:
                    reaped = True   # already reaped elsewhere / ECHILD
                    break
                if pid:
                    reaped = True
                    break
                time.sleep(0.02)
            if not reaped:
                try:
                    os.kill(self.pid, signal.SIGKILL)
                    os.waitpid(self.pid, 0)
                except OSError:
                    pass
            self.pid = -1
        if self.fd > 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self.alive = False


def _bundled_mapscii() -> str | None:
    """Return the in-repo (or wheel-shared) mapscii binary path if any.

    Checked locations, in order:
      1. ``$LIBWATCHDUTY_MAPSCII`` env override
      2. Editable / sdist checkout: ``<repo>/vendor/mapscii``
      3. Wheel-installed shared data: ``<sys.prefix>/share/libwatchduty/vendor/mapscii``
    """
    env = os.environ.get("LIBWATCHDUTY_MAPSCII")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env

    here = os.path.dirname(os.path.abspath(__file__))
    candidates: list[str] = []
    for rel in (
        os.path.join("..", "..", "..", "vendor", "mapscii"),
        os.path.join("..", "..", "vendor", "mapscii"),
        os.path.join("..", "vendor", "mapscii"),
    ):
        candidates.append(os.path.normpath(
            os.path.join(here, rel, "node_modules", ".bin", "mapscii")
        ))
    # Wheel-installed shared data path.
    candidates.append(os.path.join(
        sys.prefix, "share", "libwatchduty", "vendor", "mapscii",
        "node_modules", ".bin", "mapscii",
    ))
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _show_mapscii(stdscr, lat: float, lng: float, zoom: int = 13) -> str | None:
    """Suspend curses, shell out to `mapscii` for an interactive map view.

    Resolution order: bundled vendor/mapscii binary first, then `mapscii`
    on $PATH. Returns None on success, or an error string when neither
    is available / launch fails.
    """
    binary = _bundled_mapscii() or shutil.which("mapscii")
    if not binary:
        return (
            "mapscii not found — run `watchduty-install-mapscii` "
            "(needs node+npm)"
        )
    try:
        curses.def_prog_mode()
        curses.endwin()
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
        env = dict(os.environ)
        # Upstream mapscii main.js ignores `-l`; our vendored copy reads
        # these env vars and recenters after init.
        env["MAPSCII_LAT"] = f"{lat:.5f}"
        env["MAPSCII_LNG"] = f"{lng:.5f}"
        env["MAPSCII_ZOOM"] = str(int(zoom))
        try:
            subprocess.run(
                [binary, "-l", f"{lat:.5f},{lng:.5f},{int(zoom)}"],
                env=env, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return f"mapscii failed: {type(e).__name__}: {e}"
    finally:
        try:
            curses.reset_prog_mode()
            stdscr.clear()
            stdscr.refresh()
        except curses.error:
            pass
    return None
