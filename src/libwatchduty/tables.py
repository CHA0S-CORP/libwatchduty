"""Aligned-table, key/value, and tree renderers for libwatchduty terminal output.

Pure-stdlib helpers used by the CLI to format list and detail responses.
Width math strips ANSI escapes; color is opt-in and TTY-aware via colors.use_color.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Union

from .colors import (
    BAR_BG,
    BAR_FG_BAD,
    BAR_FG_GOOD,
    BAR_FG_MED,
    TREE_BLANK,
    TREE_BRANCH,
    TREE_LAST,
    TREE_PIPE,
    HEADING,
    paint,
    use_color,
)

# Matches CSI (ESC [ ... letter) and OSC 8 hyperlink envelopes.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Column ``get`` returns either a plain string or a (text, ansi-codes) tuple --
# but we keep the simple API: get returns a string, and the optional ``color``
# callable receives the raw value and row dict to produce SGR codes.
RowT = Any
GetCallable = Callable[[RowT], str]
ColorCallable = Callable[[Any, RowT], Optional[str]]


@dataclass
class Column:
    """Declarative spec for one rendered column.

    ``get`` may be a callable ``row -> str`` or a string field name (which is
    looked up on the row via dict-get or getattr). ``color`` may be a callable
    ``(value, row) -> sgr-codes`` (semicolon-joined string or None) or None.
    Each character is treated as visual width 1 -- adequate for ASCII and
    narrow Latin/extended Latin text. East Asian wide and ZWJ emoji sequences
    will misalign; documented limitation, sufficient for current data.
    """

    header: str
    get: Union[GetCallable, str]
    align: str = "left"  # 'left' | 'right' | 'center'
    width: Optional[int] = None
    truncate: bool = True
    color: Optional[ColorCallable] = None


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------


def _visible_len(s: str) -> int:
    """Return the printable column width of ``s`` with ANSI escapes removed.

    Strips CSI (SGR) and OSC sequences before counting characters. Treats each
    surviving codepoint as width 1; East Asian wide chars are not special-cased.
    """
    if not s:
        return 0
    stripped = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", s))
    return len(stripped)


def _strip_ansi(s: str) -> str:
    """Return ``s`` with all CSI and OSC escape sequences removed."""
    return _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", s))


def _term_width(default: int = 100) -> int:
    """Return current terminal width, falling back to ``default``."""
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except (AttributeError, OSError, ValueError):
        return default


def _resolve_get(getter: Union[GetCallable, str]) -> GetCallable:
    """Normalize a Column.get into a callable ``row -> str``."""
    if callable(getter):
        return getter
    field_name = getter

    def _g(row: RowT) -> str:
        if isinstance(row, dict):
            val = row.get(field_name)
        else:
            val = getattr(row, field_name, None)
        return "" if val is None else str(val)

    return _g


def _truncate(text: str, width: int) -> str:
    """Truncate ``text`` (visible width) to ``width`` columns with an ellipsis.

    Operates on raw text (no ANSI awareness); call sites pass uncolored cell
    text into this helper and apply color afterwards.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def _pad(text: str, width: int, align: str) -> str:
    """Pad ``text`` to ``width`` columns according to ``align``.

    Padding is computed against the visible length (ANSI-stripped), so colored
    cells line up correctly with plain ones.
    """
    vis = _visible_len(text)
    if vis >= width:
        return text
    extra = width - vis
    if align == "right":
        return (" " * extra) + text
    if align == "center":
        left = extra // 2
        right = extra - left
        return (" " * left) + text + (" " * right)
    return text + (" " * extra)


# ----------------------------------------------------------------------------
# Public renderers
# ----------------------------------------------------------------------------


def render_table(
    rows: Sequence[RowT],
    columns: Sequence[Column],
    *,
    max_width: Optional[int] = None,
    color: bool = True,
    header: bool = True,
    separator: bool = True,
) -> str:
    """Render ``rows`` as an aligned text table.

    Args:
        rows: Sequence of row objects (dicts or attribute-bearing objects).
        columns: Column specs describing how to extract, align, and color cells.
        max_width: Hard cap on total table width; defaults to terminal width.
        color: When False (or stdout is not a TTY), suppresses all ANSI codes.
        header: Emit the header row when True.
        separator: Emit a dashed underline beneath the header when True.

    Returns:
        Multi-line string (no trailing newline) ready to print.
    """
    if not columns:
        return ""

    do_color = color and use_color(sys.stdout)
    cap = max_width if max_width is not None else _term_width()

    # Pre-compute raw cell text (uncolored) for width calculation.
    getters = [_resolve_get(c.get) for c in columns]
    raw: list[list[str]] = []
    raw_values: list[list[Any]] = []
    for row in rows:
        cells: list[str] = []
        values: list[Any] = []
        for g in getters:
            try:
                v = g(row)
            except Exception:
                v = ""
            text = "" if v is None else str(v)
            cells.append(text)
            values.append(v)
        raw.append(cells)
        raw_values.append(values)

    # Natural widths: max of header and any cell.
    n = len(columns)
    natural = [_visible_len(c.header) for c in columns]
    for cells in raw:
        for i, cell in enumerate(cells):
            w = _visible_len(cell)
            if w > natural[i]:
                natural[i] = w

    # Per-column target: clamp to width hint when provided.
    widths = list(natural)
    for i, c in enumerate(columns):
        if c.width is not None:
            widths[i] = min(widths[i], c.width) if c.truncate else max(widths[i], 0) or natural[i]
            # If a width hint is present and truncate is on, also expand to at
            # least header width so the header is never clipped beyond hint.
            widths[i] = max(widths[i], min(_visible_len(c.header), c.width))

    # Fit to cap (account for single-space column separators).
    gutter = 1
    overhead = gutter * max(0, n - 1)
    total = sum(widths) + overhead
    if total > cap:
        # Shrink truncatable columns proportionally to their slack above header.
        excess = total - cap
        shrinkable = [
            i for i, c in enumerate(columns)
            if c.truncate and widths[i] > _visible_len(c.header)
        ]
        # Iterate until excess is absorbed or no slack remains.
        guard = 0
        while excess > 0 and shrinkable and guard < 10000:
            guard += 1
            # Find the widest shrinkable column and trim one.
            shrinkable.sort(key=lambda i: widths[i], reverse=True)
            i = shrinkable[0]
            min_w = max(1, _visible_len(columns[i].header))
            if widths[i] <= min_w:
                shrinkable.remove(i)
                continue
            widths[i] -= 1
            excess -= 1

    # Build lines.
    out_lines: list[str] = []

    def _color_codes(c: Column, value: Any, row: RowT) -> Optional[str]:
        if not do_color or c.color is None:
            return None
        try:
            return c.color(value, row)
        except Exception:
            return None

    if header:
        parts: list[str] = []
        for i, c in enumerate(columns):
            text = _truncate(c.header, widths[i])
            if do_color:
                text = paint(text, HEADING)
            parts.append(_pad(text, widths[i], c.align))
        out_lines.append((" " * gutter).join(parts).rstrip())

        if separator:
            sep_parts = ["-" * widths[i] for i in range(n)]
            sep_line = (" " * gutter).join(sep_parts).rstrip()
            if do_color:
                sep_line = paint(sep_line, HEADING)
            out_lines.append(sep_line)

    for cells, values, row in zip(raw, raw_values, rows):
        parts = []
        for i, c in enumerate(columns):
            text = cells[i]
            if c.truncate:
                text = _truncate(text, widths[i])
            codes = _color_codes(c, values[i], row)
            if codes:
                text_colored = paint(text, codes)
            else:
                text_colored = text
            parts.append(_pad(text_colored, widths[i], c.align))
        out_lines.append((" " * gutter).join(parts).rstrip())

    return "\n".join(out_lines)


def render_kv(items: Sequence[tuple[str, Any]], *, color: bool = True) -> str:
    """Render a list of ``(label, value)`` pairs as a two-column block.

    Args:
        items: Sequence of ``(label, value)`` tuples. Values are str()-ified.
        color: When False (or stdout is not a TTY), suppresses ANSI codes.

    Returns:
        Multi-line string with each label right-padded to a common width and a
        single ``" : "`` separator. Long values wrap to the terminal with a
        hanging indent that lines up beneath the value column.
    """
    if not items:
        return ""
    do_color = color and use_color(sys.stdout)

    label_w = max(_visible_len(str(label)) for label, _ in items)
    sep = " : "
    cap = _term_width()
    value_col = label_w + len(sep)
    value_w = max(10, cap - value_col)
    indent = " " * value_col

    lines: list[str] = []
    for label, value in items:
        label_text = str(label)
        if do_color:
            label_text = paint(label_text, HEADING)
        label_pad = _pad(label_text, label_w, "right")

        value_text = "" if value is None else str(value)
        # Wrap on visible width while preserving any embedded ANSI codes by
        # operating on the stripped form for measurement and the raw text for
        # output. When the raw value already contains escapes, we fall back to
        # a single un-wrapped line to avoid breaking sequences.
        plain = _strip_ansi(value_text)
        if plain == value_text and _visible_len(value_text) > value_w:
            wrapped = _wrap_plain(value_text, value_w)
            first, *rest = wrapped
            lines.append(f"{label_pad}{sep}{first}")
            for piece in rest:
                lines.append(f"{indent}{piece}")
        else:
            lines.append(f"{label_pad}{sep}{value_text}")

    return "\n".join(lines)


def _wrap_plain(text: str, width: int) -> list[str]:
    """Soft-wrap plain ``text`` to ``width`` columns, splitting on whitespace."""
    if width <= 0:
        return [text]
    out: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if len(paragraph) <= width:
            out.append(paragraph)
            continue
        words = paragraph.split(" ")
        cur = ""
        for w in words:
            if not cur:
                # Hard-split tokens longer than the column.
                while len(w) > width:
                    out.append(w[:width])
                    w = w[width:]
                cur = w
                continue
            if len(cur) + 1 + len(w) <= width:
                cur = f"{cur} {w}"
            else:
                out.append(cur)
                while len(w) > width:
                    out.append(w[:width])
                    w = w[width:]
                cur = w
        if cur:
            out.append(cur)
    return out or [""]


def render_tree(
    parent_lines: Sequence[str],
    children_per_parent: Sequence[Sequence[str]],
) -> str:
    """Render a two-level tree with box-drawing glyphs.

    Args:
        parent_lines: Already-formatted parent rows (one string each).
        children_per_parent: For each parent, an ordered list of pre-formatted
            child lines. Outer length must match ``parent_lines``.

    Returns:
        Multi-line string where each parent is followed by its children, each
        child prefixed with ``├─`` (or ``└─`` for the last). Parents with no
        children are emitted alone.
    """
    if len(parent_lines) != len(children_per_parent):
        raise ValueError(
            "render_tree: parent_lines and children_per_parent length mismatch"
        )
    lines: list[str] = []
    for parent, kids in zip(parent_lines, children_per_parent):
        lines.append(parent)
        last_i = len(kids) - 1
        for i, kid in enumerate(kids):
            glyph = TREE_LAST if i == last_i else TREE_BRANCH
            # Indent any continuation lines inside ``kid`` under the glyph.
            kid_lines = kid.splitlines() or [""]
            cont_prefix = TREE_BLANK if i == last_i else TREE_PIPE
            lines.append(f"{glyph}{kid_lines[0]}")
            for extra in kid_lines[1:]:
                lines.append(f"{cont_prefix}{extra}")
    return "\n".join(lines)


# Sub-character block ramp for smooth progress-bar interpolation.
_BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"


def render_bar(
    value: float,
    total: float = 100.0,
    *,
    width: int = 12,
    show_pct: bool = True,
    color: bool = True,
) -> str:
    """Render a Unicode block progress bar like ``█████▌░░░░░ 45%``.

    Args:
        value: filled portion (clamped to [0, total]).
        total: bar maximum (default 100, so value reads as a percent).
        width: total column count for the bar glyph area.
        show_pct: append the integer percent after the bar.
        color: honor the colors palette; falls back to plain when off or
            when colors.use_color() is False.

    Color thresholds: <30% → red, 30–69% → amber, ≥70% → green.
    """
    if total <= 0 or width <= 0:
        return ""
    v = max(0.0, min(float(value), float(total)))
    ratio = v / total
    eighths = int(round(ratio * width * 8))
    full = eighths // 8
    rem = eighths % 8
    bar = "█" * full
    if rem and full < width:
        bar += _BAR_EIGHTHS[rem]
        full += 1
    bar += "░" * (width - full)
    if color and use_color():
        pct = ratio * 100
        fg = (
            BAR_FG_GOOD if pct >= 70
            else BAR_FG_MED if pct >= 30
            else BAR_FG_BAD
        )
        # Paint filled vs empty separately so the empty trough reads as dim.
        cut = full
        filled = paint(bar[:cut], fg)
        empty = paint(bar[cut:], BAR_BG)
        bar = filled + empty
    out = bar
    if show_pct:
        out += f" {int(round(ratio * 100))}%"
    return out


__all__ = [
    "Column",
    "render_table",
    "render_kv",
    "render_tree",
    "render_bar",
]
