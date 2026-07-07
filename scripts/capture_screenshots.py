"""Capture deterministic screenshots of the libwatchduty TUI.

Renders the four detail tabs (updates / radio / map / evac) at three
predefined terminal sizes against a synthetic, fully-populated
``_TuiState`` — no live API calls, no real ``WatchDutyClient``. Output
goes under ``docs/screenshots/``:

* ``ansi/<rows>x<cols>-<tab>.txt`` — frame with SGR colors, one per file
* ``png/<rows>x<cols>-<tab>.png`` — same frame rendered via Pillow + a
  monospace font, if Pillow is importable. Silently warns and skips PNG
  output otherwise.

The TUI draw functions need a curses context to compute attributes, so
each frame is rendered inside a child process forked under a PTY of the
requested size. The parent feeds the PTY output through a ``pyte``
``Screen`` to extract the rendered grid + per-cell colors. The parent
process itself does not need to be a TTY.

Run directly:

    python -m scripts.capture_screenshots

The script exits non-zero if it fails to produce *any* ANSI dump.
"""

from __future__ import annotations

import argparse
import errno
import os
import pty
import select
import struct
import sys
import termios
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    import pyte

# Make sure ``src/`` is importable when running this script straight out
# of a source checkout (no install). We append rather than insert so an
# editable install (``pip install -e .``) still wins.
_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.append(str(_SRC))


# ---------------------------------------------------------------------------
# sizes + tabs
# ---------------------------------------------------------------------------

# (rows, cols)
_SIZES: tuple[tuple[int, int], ...] = (
    (24, 100),
    (40, 160),
    (60, 200),
)

_TABS: tuple[str, ...] = ("updates", "radio", "map", "evac")


# ---------------------------------------------------------------------------
# synthetic state
# ---------------------------------------------------------------------------


def _now_iso(offset_seconds: float = 0.0) -> str:
    """Stable-ish ISO timestamp used for the seeded fixture."""
    # Use a fixed wall clock so screenshots are reproducible across runs.
    base = 1_750_000_000.0 + offset_seconds  # ~2025-06-15 UTC
    t = time.gmtime(base)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


def _build_fires() -> list[dict]:
    """Synthetic but realistic ``geo_event`` dicts spanning a few states."""
    return [
        {
            "id": 104994,
            "name": "Pine Ridge Fire",
            "address": "Mendocino County, CA",
            "lat": 39.3076,
            "lng": -123.1986,
            "is_active": True,
            "is_prescribed": False,
            "date_created": _now_iso(-12 * 3600),
            "date_modified": _now_iso(-180),
            "geo_event_type": "wildfire",
            "data": {
                "acreage": 4321.0,
                "containment": 35,
                "evacuation_orders": (
                    "<ul>"
                    "<li>Zone MEN-1A: Pine Ridge Rd between mile 4 and 7.</li>"
                    "<li>Zone MEN-1B: All of Hopland Estates east of Hwy 101.</li>"
                    "</ul>"
                ),
                "evacuation_warnings": (
                    "<p>Zone MEN-2C: residents should be ready to leave "
                    "on short notice.</p>"
                ),
            },
        },
        {
            "id": 104995,
            "name": "Granite Peak Complex",
            "address": "Tehama County, CA",
            "lat": 40.1190,
            "lng": -122.2358,
            "is_active": True,
            "is_prescribed": False,
            "date_created": _now_iso(-26 * 3600),
            "date_modified": _now_iso(-900),
            "geo_event_type": "wildfire",
            "data": {
                "acreage": 18750.5,
                "containment": 10,
                "evacuation_orders": (
                    "<ul><li>TEH-3</li><li>TEH-4</li></ul>"
                ),
            },
        },
        {
            "id": 104996,
            "name": "Silverado Hills",
            "address": "Orange County, CA",
            "lat": 33.7407,
            "lng": -117.6312,
            "is_active": True,
            "is_prescribed": False,
            "date_created": _now_iso(-3 * 3600),
            "date_modified": _now_iso(-60),
            "geo_event_type": "wildfire",
            "data": {
                "acreage": 612.0,
                "containment": 60,
            },
        },
        {
            "id": 104997,
            "name": "Coyote Creek Rx",
            "address": "Stanislaus NF, CA",
            "lat": 38.1234,
            "lng": -119.9876,
            "is_active": True,
            "is_prescribed": True,
            "date_created": _now_iso(-2 * 24 * 3600),
            "date_modified": _now_iso(-3 * 3600),
            "geo_event_type": "wildfire",
            "data": {
                "acreage": 230.0,
                "containment": 100,
            },
        },
        {
            "id": 104998,
            "name": "Russian River Flood Watch",
            "address": "Sonoma County, CA",
            "lat": 38.5102,
            "lng": -122.9647,
            "is_active": True,
            "is_prescribed": False,
            "date_created": _now_iso(-6 * 3600),
            "date_modified": _now_iso(-1500),
            "geo_event_type": "flood",
            "data": {
                "acreage": None,
                "containment": None,
                "evacuation_warnings": (
                    "<p>SON-FLOOD-1: residents along the lower Russian "
                    "River corridor should monitor conditions.</p>"
                ),
            },
        },
    ]


def _build_reports(fire_id: int) -> list[dict]:
    """A handful of update reports for the Updates tab."""
    return [
        {
            "id": fire_id * 100 + 1,
            "date_created": _now_iso(-180),
            "user_created": {"display_name": "WD Reporter (Marin)"},
            "message": (
                "<p>Forward progress stopped on the western flank near "
                "Pine Ridge Rd. Resources: 4 Type 1 engines, 2 hand "
                "crews, 1 air tanker, 1 helicopter.</p>"
            ),
            "media": [],
        },
        {
            "id": fire_id * 100 + 2,
            "date_created": _now_iso(-900),
            "user_created": {"display_name": "CalFire MEU"},
            "message": (
                "Evacuation order issued for Zone MEN-1A and MEN-1B. "
                "Shelter open at Ukiah Fairgrounds."
            ),
            "media": [],
        },
        {
            "id": fire_id * 100 + 3,
            "date_created": _now_iso(-3600),
            "user_created": {"display_name": "WD Reporter (Sonoma)"},
            "message": (
                "Smoke column visible from Hwy 101. Spotting reported "
                "1/4 mile ahead. Air attack inbound from McClellan."
            ),
            "media": [],
        },
    ]


def _build_radio(fire_id: int) -> list[dict]:
    """Scanner feeds for the Radio tab."""
    return [
        {
            "id": 1,
            "name": "Mendocino County Fire / EMS",
            "listen_url": "https://broadcastify.example/12345",
            "listeners": 142,
        },
        {
            "id": 2,
            "name": "CDF Howard Forest Command",
            "listen_url": "https://broadcastify.example/12346",
            "listeners": 87,
        },
        {
            "id": 3,
            "name": "Ukiah PD / Sheriff",
            "listen_url": "https://broadcastify.example/12347",
            "listeners": 31,
        },
    ]


def _populate_state(state) -> None:
    """Cram the ``_TuiState`` with fixture data + recompute derived fields."""
    from libwatchduty import tui as _tui

    state.fires = _build_fires()
    state.near = (37.7749, -122.4194)  # SF, so distances are reasonable
    state.near_source = "fixture"
    state.within_km = 500.0
    state.sort_key = "threat"

    for f in state.fires:
        fid = int(f["id"])
        state.reports_cache[fid] = _build_reports(fid)
        state.radio_cache[fid] = _build_radio(fid)
        state.cameras_cache[fid] = []
        state.fps_cache[fid] = []
        # Seed acreage history so the sparkline + delta render.
        ac = float((f.get("data") or {}).get("acreage") or 0.0)
        if ac:
            state.acreage_history[fid] = [
                (1_750_000_000.0 - 3600 * i, max(1.0, ac * (1.0 - 0.04 * i)))
                for i in range(6, 0, -1)
            ]

    _tui._recompute_distances(state)
    _tui._recompute_threats(state)
    _tui._recompute_visible(state)
    # Select something with rich data (Pine Ridge).
    for i, f in enumerate(state.visible_fires):
        if int(f["id"]) == 104994:
            state.selected_idx = i
            break


# ---------------------------------------------------------------------------
# child-side: render one frame inside curses
# ---------------------------------------------------------------------------


def _child_render(rows: int, cols: int, tab: str) -> None:
    """Entry point inside the forked child. Draws once, then exits."""
    import curses

    from libwatchduty import tui as _tui

    state = _tui._TuiState()
    _populate_state(state)
    state.active_tab = tab

    def _draw(stdscr) -> None:
        curses.curs_set(0)
        holder: dict = {}
        _tui._init_colors(holder)
        stdscr.erase()
        layout = _tui._compute_layout(rows, cols)
        if layout.too_small:
            stdscr.addnstr(
                0, 0,
                f"terminal too small ({cols}x{rows})", cols,
            )
        else:
            _tui._draw_header(stdscr, state, layout, holder)
            _tui._draw_list(stdscr, state, layout, holder)
            _tui._draw_detail(stdscr, state, layout, holder)
            _tui._draw_footer(stdscr, state, layout, holder)
        stdscr.noutrefresh()
        curses.doupdate()
        # Give the PTY a beat to flush before tearing curses down.
        time.sleep(0.15)

    curses.wrapper(_draw)


# ---------------------------------------------------------------------------
# parent-side: pty driver + pyte capture
# ---------------------------------------------------------------------------


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """ioctl TIOCSWINSZ on ``fd`` — must happen before the child runs."""
    try:
        TIOCSWINSZ = termios.TIOCSWINSZ  # type: ignore[attr-defined]
    except AttributeError:
        TIOCSWINSZ = 0x5414  # Linux fallback
    size = struct.pack("HHHH", rows, cols, 0, 0)
    import fcntl
    fcntl.ioctl(fd, TIOCSWINSZ, size)


def _capture_pty(
    rows: int, cols: int, tab: str,
) -> tuple["pyte.Screen", int]:
    """Fork a child that renders once.

    Returns ``(screen, child_status)`` where ``child_status`` is the raw
    ``os.waitpid`` status word (0 == clean exit).
    """
    import pyte

    pid, fd = pty.fork()
    if pid == 0:
        # Child. Reset stdio to the slave PTY (pty.fork already did this).
        # Force a sane terminal type so curses initializes 256-color paths.
        os.environ["TERM"] = "xterm-256color"
        os.environ["LANG"] = os.environ.get("LANG") or "en_US.UTF-8"
        os.environ["LC_ALL"] = os.environ.get("LC_ALL") or "en_US.UTF-8"
        try:
            _child_render(rows, cols, tab)
        except BaseException as exc:  # pragma: no cover — surfaces via parent
            try:
                sys.stderr.write(f"child render failed: {exc!r}\n")
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(1)
        os._exit(0)

    # Parent.
    _set_winsize(fd, rows, cols)
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    deadline = time.monotonic() + 8.0
    child_status = 0
    reaped = False
    while True:
        if time.monotonic() > deadline:
            break
        rlist, _, _ = select.select([fd], [], [], 0.25)
        if rlist:
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            stream.feed(chunk)
        # Reap the child once it's done writing.
        done_pid, status = os.waitpid(pid, os.WNOHANG)
        if done_pid:
            child_status = status
            reaped = True
            # Drain any final bytes.
            try:
                while True:
                    rlist, _, _ = select.select([fd], [], [], 0.05)
                    if not rlist:
                        break
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    stream.feed(chunk)
            except OSError:
                pass
            break

    try:
        os.close(fd)
    except OSError:
        pass
    if not reaped:
        try:
            _, child_status = os.waitpid(pid, 0)
        except ChildProcessError:
            child_status = 0

    return screen, child_status


# ---------------------------------------------------------------------------
# pyte → ANSI + PNG
# ---------------------------------------------------------------------------


# pyte returns color names ('default', 'red', 'brown', ...) and either
# named or hex strings for 256-color cells. Map known names → ANSI/RGB.
_NAMED_FG = {
    "black": 30, "red": 31, "green": 32, "brown": 33,
    "blue": 34, "magenta": 35, "cyan": 36, "white": 37,
    "brightblack": 90, "brightred": 91, "brightgreen": 92,
    "brightbrown": 93, "brightblue": 94, "brightmagenta": 95,
    "brightcyan": 96, "brightwhite": 97,
}
_NAMED_BG = {k: v + 10 for k, v in _NAMED_FG.items()}

_NAMED_RGB = {
    "black": (0, 0, 0),
    "red": (205, 49, 49),
    "green": (13, 188, 121),
    "brown": (229, 229, 16),  # yellow-ish; pyte calls it brown
    "blue": (36, 114, 200),
    "magenta": (188, 63, 188),
    "cyan": (17, 168, 205),
    "white": (229, 229, 229),
    "brightblack": (102, 102, 102),
    "brightred": (241, 76, 76),
    "brightgreen": (35, 209, 139),
    "brightbrown": (245, 245, 67),
    "brightblue": (59, 142, 234),
    "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219),
    "brightwhite": (255, 255, 255),
    "default": (220, 220, 220),
}


def _hex_to_rgb(s: str) -> tuple[int, int, int] | None:
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            return None
    return None


def _fg_ansi(color: str) -> str:
    if not color or color == "default":
        return "39"
    rgb = _hex_to_rgb(color)
    if rgb:
        return f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}"
    code = _NAMED_FG.get(color)
    if code is not None:
        return str(code)
    return "39"


def _bg_ansi(color: str) -> str:
    if not color or color == "default":
        return "49"
    rgb = _hex_to_rgb(color)
    if rgb:
        return f"48;2;{rgb[0]};{rgb[1]};{rgb[2]}"
    code = _NAMED_BG.get(color)
    if code is not None:
        return str(code)
    return "49"


def _fg_rgb(color: str) -> tuple[int, int, int]:
    if not color or color == "default":
        return _NAMED_RGB["default"]
    rgb = _hex_to_rgb(color)
    if rgb:
        return rgb
    return _NAMED_RGB.get(color, _NAMED_RGB["default"])


def _bg_rgb(color: str) -> tuple[int, int, int]:
    if not color or color == "default":
        return (15, 19, 24)  # match _BG_256["panel_bg"] for visual parity
    rgb = _hex_to_rgb(color)
    if rgb:
        return rgb
    return _NAMED_RGB.get(color, (15, 19, 24))


def _screen_to_ansi(screen) -> str:
    """Serialize a pyte ``Screen`` to an ANSI string with SGR colors."""
    out: list[str] = []
    for y in range(screen.lines):
        last_sgr: str | None = None
        for x in range(screen.columns):
            ch = screen.buffer[y][x]
            sgr_parts: list[str] = []
            if ch.bold:
                sgr_parts.append("1")
            if ch.italics:
                sgr_parts.append("3")
            if ch.underscore:
                sgr_parts.append("4")
            if ch.reverse:
                sgr_parts.append("7")
            sgr_parts.append(_fg_ansi(ch.fg))
            sgr_parts.append(_bg_ansi(ch.bg))
            sgr = ";".join(sgr_parts)
            if sgr != last_sgr:
                out.append(f"\x1b[0;{sgr}m")
                last_sgr = sgr
            out.append(ch.data or " ")
        out.append("\x1b[0m\n")
    return "".join(out)


def _screen_to_png(screen, path: Path) -> bool:
    """Render ``screen`` to PNG. Returns False if Pillow is missing."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False

    # Pick a monospaced font that ships on macOS / most Linuxes. Pillow
    # falls back to its default bitmap font if every path misses.
    font: "ImageFont.ImageFont"
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/Library/Fonts/Andale Mono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    ]
    font_size = 14
    font = None  # type: ignore[assignment]
    for c in candidates:
        if os.path.exists(c):
            try:
                font = ImageFont.truetype(c, font_size)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()

    # Cell metrics. Use the bounding box of a wide glyph for the width
    # so most monospaced fonts produce a tight grid.
    try:
        left, top, r, b = font.getbbox("M")
        cw = max(1, r - left)
        ch = max(1, b - top)
    except AttributeError:  # very old Pillow
        cw, ch = font.getsize("M")  # type: ignore[attr-defined]

    # A little vertical breathing room for descenders.
    ch = int(ch * 1.25)

    img_w = cw * screen.columns
    img_h = ch * screen.lines
    img = Image.new("RGB", (img_w, img_h), (10, 12, 16))
    draw = ImageDraw.Draw(img)

    for y in range(screen.lines):
        for x in range(screen.columns):
            cell = screen.buffer[y][x]
            fg = _fg_rgb(cell.fg)
            bg = _bg_rgb(cell.bg)
            if cell.reverse:
                fg, bg = bg, fg
            px = x * cw
            py = y * ch
            if bg != (10, 12, 16):
                draw.rectangle([px, py, px + cw, py + ch], fill=bg)
            data = cell.data or " "
            if data.strip():
                try:
                    draw.text((px, py), data, fill=fg, font=font)
                except Exception:
                    # Some glyphs (CJK, combining) may not render — skip.
                    pass

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return True


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


@dataclass
class _CaptureResult:
    rows: int
    cols: int
    tab: str
    ansi_path: Path
    png_path: Path | None
    ansi_ok: bool
    png_ok: bool
    error: str | None = None


def _run_one(
    rows: int, cols: int, tab: str,
    ansi_dir: Path, png_dir: Path,
    write_png: bool,
) -> _CaptureResult:
    stem = f"{rows:02d}x{cols:03d}-{tab}"
    ansi_path = ansi_dir / f"{stem}.txt"
    png_path = png_dir / f"{stem}.png" if write_png else None
    try:
        screen, child_status = _capture_pty(rows, cols, tab)
    except Exception as exc:
        return _CaptureResult(
            rows, cols, tab, ansi_path, png_path,
            ansi_ok=False, png_ok=False, error=f"capture: {exc!r}",
        )

    child_err: str | None = None
    if os.WIFEXITED(child_status):
        code = os.WEXITSTATUS(child_status)
        if code != 0:
            child_err = f"child exit={code}"
    elif os.WIFSIGNALED(child_status):
        child_err = f"child signal={os.WTERMSIG(child_status)}"

    ansi_ok = False
    png_ok = False
    err = None
    try:
        ansi_dir.mkdir(parents=True, exist_ok=True)
        ansi_path.write_text(_screen_to_ansi(screen), encoding="utf-8")
        ansi_ok = True
    except Exception as exc:
        err = f"ansi: {exc!r}"

    if write_png and png_path is not None:
        try:
            png_ok = _screen_to_png(screen, png_path)
        except Exception as exc:
            err = (err + " | " if err else "") + f"png: {exc!r}"

    if child_err:
        err = (err + " | " if err else "") + child_err

    return _CaptureResult(
        rows, cols, tab, ansi_path, png_path,
        ansi_ok=ansi_ok and not child_err,
        png_ok=png_ok,
        error=err,
    )


def _maybe_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        warnings.warn(
            "Pillow not installed — skipping PNG output (pip install Pillow)",
            RuntimeWarning,
            stacklevel=2,
        )
        return False


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=_REPO / "docs" / "screenshots",
        help="Output directory (default: docs/screenshots/)",
    )
    parser.add_argument(
        "--no-png",
        action="store_true",
        help="Skip PNG rendering even if Pillow is installed.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    out_root: Path = args.out
    ansi_dir = out_root / "ansi"
    png_dir = out_root / "png"

    write_png = (not args.no_png) and _maybe_pillow()

    results: list[_CaptureResult] = []
    for rows, cols in _SIZES:
        for tab in _TABS:
            res = _run_one(
                rows, cols, tab, ansi_dir, png_dir, write_png=write_png,
            )
            results.append(res)
            tag = "ok" if res.ansi_ok else "FAIL"
            extra = ""
            if write_png:
                extra = f" png={'ok' if res.png_ok else 'skip'}"
            if res.error:
                extra += f" err={res.error}"
            print(
                f"[{tag}] {rows}x{cols} {tab:<7} → {res.ansi_path}{extra}",
                file=sys.stderr,
            )

    any_ansi = any(r.ansi_ok for r in results)
    failures = [r for r in results if not r.ansi_ok]
    print(
        f"\n{sum(1 for r in results if r.ansi_ok)}/{len(results)} ANSI frames written.",
        file=sys.stderr,
    )
    if failures:
        print(f"{len(failures)} failures:", file=sys.stderr)
        for r in failures:
            print(
                f"  {r.rows}x{r.cols} {r.tab}: {r.error}", file=sys.stderr,
            )
    return 0 if any_ansi else 1


if __name__ == "__main__":
    raise SystemExit(main())
