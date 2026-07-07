"""Interactive curses TUI for libwatchduty (redesigned).

POSIX-only. Stdlib-only at the view layer: :mod:`curses`, :mod:`threading`,
:mod:`queue`, :mod:`time`, :mod:`locale`, :mod:`html`, :mod:`re`,
:mod:`signal`, :mod:`textwrap`. Network I/O goes through
:class:`libwatchduty.client.WatchDutyClient` (which uses ``requests``); all
calls stay on a worker thread.

Layout (matches design_handoff_wildfire_tui/README.md):

    ┌──────────────────────────────────────────────────────────┐
    │ status bar (◉ watchduty · ⌖ near · sort ▼ THREAT · LIVE) │
    ├──────────────────────┬───────────────────────────────────┤
    │ FIRE LIST (~42%)     │ DETAIL PANE (~58%)                │
    │ THREAT|DIR|DIST|...  │  title · KV (threat,wind,spark…)  │
    │ ▰ bar + bearing ↗    │  tabs: Updates / Radio / Map/Evac │
    │ legend row           │  panel (wrapped, scrollbar right) │
    ├──────────────────────┴───────────────────────────────────┤
    │ keybind footer                                            │
    └──────────────────────────────────────────────────────────┘

Entry point: :func:`run` — launches the curses app loop.
"""


from __future__ import annotations

# Compatibility facade: `from libwatchduty import tui` predates the package
# split, so every name callers (cli, install_mapscii, scripts/, tests/)
# reach for is re-exported here, grouped by source module.

from .state import (
    _FLASH_TTL,
    _FOCUS_DETAIL,
    _FOCUS_LIST,
    _HISTORY_LIMIT,
    _IMG_SIZE_ORDER,
    _IMG_SIZE_PRESETS,
    _LIST_ROWS_PER_FIRE,
    _LIST_W_MAX,
    _MIN_AUTO_REFRESH,
    _MIN_COLS,
    _MIN_LINES,
    _REPORTS_CACHE_MAX,
    _SORT_KEYS,
    _SPARK_RAMP,
    _TAB_KEYS,
    _TABS,
    _TuiState,
)
from .helpers import (
    _bearing_arrow,
    _bearing_compass,
    _format_age,
    _haversine_km,
    _initial_bearing,
    _is_planned,
    _safe_str,
    _seconds_since_iso,
    _sparkline,
    _split_html_lines,
    _strip_html,
    _threat_bar_glyphs,
    _threat_factors,
    _threat_tier,
    _wrap_around_image,
)
from .palette import (
    _attr,
    _embed_pair_attr,
    _hex_to_curses,
    _init_colors,
    _on_bg,
    _pyte_color_index,
)
from .layout import _Layout, _addnstr, _compute_layout
from .derive import (
    _jump_to_next_match,
    _recompute_distances,
    _recompute_threats,
    _recompute_visible,
    _record_histories,
    _select_idx,
    _set_status,
)
from .worker import (
    _bulk_prefetch_visible,
    _enqueue_aircraft_catalog,
    _enqueue_image,
    _enqueue_refresh,
    _enqueue_reports,
    _prefetch_for_selection,
    _worker_loop,
)
from .draw_list import (
    _LIST_DIR_W,
    _LIST_NAME_X,
    _LIST_PAD_L,
    _LIST_SUB_CONT_OFF,
    _LIST_SUB_CONT_W,
    _LIST_SUB_DIST_OFF,
    _LIST_SUB_DIST_W,
    _LIST_SUB_SIZE_OFF,
    _LIST_SUB_SIZE_W,
    _LIST_THREAT_W,
    _draw_chip,
    _draw_containment_cell,
    _draw_list,
    _draw_threat_cell,
)
from .draw_detail import (
    _camera_rect,
    _draw_detail,
    _evac_count,
    _range_rings_km,
)
from .chrome import (
    _draw_footer,
    _draw_header,
    _draw_help_overlay,
    _refresh_meter,
)
from .images_paint import (
    _clear_inline_images,
    _ensure_header_image,
    _nearby_cameras,
    _notify_new_updates,
    _paint_header_image,
    _paint_update_images,
    _pick_image_url,
    _show_image_preview,
)
from .mapscii_embed import (
    _MapsciiEmbed,
    _bundled_mapscii,
    _mercator_pixel,
    _show_mapscii,
)
from .input import (
    _apply_command,
    _handle_cmd_key,
    _handle_filter_key,
    _handle_key,
    _handle_mouse,
)
from .app import _app, _drain_results, run

__all__ = [
    "_FLASH_TTL",
    "_FOCUS_DETAIL",
    "_FOCUS_LIST",
    "_HISTORY_LIMIT",
    "_IMG_SIZE_ORDER",
    "_IMG_SIZE_PRESETS",
    "_LIST_ROWS_PER_FIRE",
    "_LIST_W_MAX",
    "_MIN_AUTO_REFRESH",
    "_MIN_COLS",
    "_MIN_LINES",
    "_REPORTS_CACHE_MAX",
    "_SORT_KEYS",
    "_SPARK_RAMP",
    "_TAB_KEYS",
    "_TABS",
    "_TuiState",
    "_bearing_arrow",
    "_bearing_compass",
    "_format_age",
    "_haversine_km",
    "_initial_bearing",
    "_is_planned",
    "_safe_str",
    "_seconds_since_iso",
    "_sparkline",
    "_split_html_lines",
    "_strip_html",
    "_threat_bar_glyphs",
    "_threat_factors",
    "_threat_tier",
    "_wrap_around_image",
    "_attr",
    "_embed_pair_attr",
    "_hex_to_curses",
    "_init_colors",
    "_on_bg",
    "_pyte_color_index",
    "_Layout",
    "_addnstr",
    "_compute_layout",
    "_jump_to_next_match",
    "_recompute_distances",
    "_recompute_threats",
    "_recompute_visible",
    "_record_histories",
    "_select_idx",
    "_set_status",
    "_bulk_prefetch_visible",
    "_enqueue_aircraft_catalog",
    "_enqueue_image",
    "_enqueue_refresh",
    "_enqueue_reports",
    "_prefetch_for_selection",
    "_worker_loop",
    "_LIST_DIR_W",
    "_LIST_NAME_X",
    "_LIST_PAD_L",
    "_LIST_SUB_CONT_OFF",
    "_LIST_SUB_CONT_W",
    "_LIST_SUB_DIST_OFF",
    "_LIST_SUB_DIST_W",
    "_LIST_SUB_SIZE_OFF",
    "_LIST_SUB_SIZE_W",
    "_LIST_THREAT_W",
    "_draw_chip",
    "_draw_containment_cell",
    "_draw_list",
    "_draw_threat_cell",
    "_camera_rect",
    "_draw_detail",
    "_evac_count",
    "_range_rings_km",
    "_draw_footer",
    "_draw_header",
    "_draw_help_overlay",
    "_refresh_meter",
    "_clear_inline_images",
    "_ensure_header_image",
    "_nearby_cameras",
    "_notify_new_updates",
    "_paint_header_image",
    "_paint_update_images",
    "_pick_image_url",
    "_show_image_preview",
    "_MapsciiEmbed",
    "_bundled_mapscii",
    "_mercator_pixel",
    "_show_mapscii",
    "_apply_command",
    "_handle_cmd_key",
    "_handle_filter_key",
    "_handle_key",
    "_handle_mouse",
    "_app",
    "_drain_results",
    "run",
]
