"""Mutable UI state (:class:`_TuiState`) + all tuning constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..client import GEO_EVENT_TYPES


# Be polite to api.watchduty.org.
_MIN_AUTO_REFRESH = 30
_LIVE_POLL_SECONDS = 30
# Bulk-sweep visible set only when the list is small. Above this threshold
# we lazy-load just the selected fire + a neighborhood window.
_BULK_PREFETCH_THRESHOLD = 24
# Neighborhood prefetch radius (reports only) around the selected fire —
# keeps j/k navigation instant on large lists without hammering the API.
_NEIGHBOR_REPORT_WINDOW = 4
# Hard cap on cached report lists; oldest dropped FIFO when exceeded.
_REPORTS_CACHE_MAX = 200
# Same idea for per-report chip extractions and raw image bytes — both grew
# unbounded over long sessions (image entries are full JPEGs, ~100 KB each).
_CHIP_CACHE_MAX = 512
_IMAGE_CACHE_MAX = 32
# "NEW" flash chips auto-expire after this many seconds.
_FLASH_TTL = 30.0
_MIN_LINES = 10
_MIN_COLS = 40
_REPORTS_RENDER_LIMIT = 40
# Spec: updates feed content capped at `max-width: 760px` (~96 cells).
_DETAIL_MAX_CONTENT = 96
# Spec: list ≈ 42% but capped so wide terminals don't leave dead gutter.
_LIST_W_MAX = 64
# Per-card layout: 1 fire = 2 rows (zebra stripe gives separation, no spacer).
_LIST_ROWS_PER_FIRE = 2

# Image-size presets for inline thumbnails (slot_h, slot_w_max).
_IMG_SIZE_PRESETS: dict[str, tuple[int, int]] = {
    "small": (6,  32),
    "med":   (10, 60),
    "large": (16, 84),
}
_IMG_SIZE_ORDER = ("small", "med", "large")
_ERROR_TTL = 5.0
_CHORD_TIMEOUT = 0.5
_TICK_MS = 200
_HISTORY_LIMIT = 24  # sparkline data points per fire

# Threat scoring knobs (per README §Threat Scoring).
_SIZE_REF = 1000.0
_GROWTH_GAIN = 0.5
_WIND_GAIN = 0.04

# Sort cycle. "threat" is the default when --near is set.
_SORT_KEYS = ("threat", "distance", "acreage", "updated")

# Detail-pane tabs + hotkey jumpers (1-4 + the spec's "keep old hotkeys").
_TABS = ("updates", "radio", "map", "evac")
# NOTE the 'c' overlap: when the Map tab is active with an embedded
# mapscii, _handle_key forwards 'c' to the mapscii PTY *before* the
# tab-jump lookup runs, so the "jump to map" binding below never
# fires in that state — intentional, and ordering-dependent.
_TAB_KEYS: dict[int, str] = {
    ord("1"): "updates", ord("2"): "radio",
    ord("3"): "map",     ord("4"): "evac",
    ord("u"): "updates", ord("R"): "radio",
    ord("c"): "map",     ord("e"): "evac",
}


# Focus regions.
_FOCUS_LIST = "list"
_FOCUS_DETAIL = "detail"

# Glyphs.
_THREAT_FULL = "▰"
_THREAT_EMPTY = "▱"
_SPARK_RAMP = "▁▂▃▄▅▆▇█"
_ARROWS = ("↑", "↗", "→", "↘", "↓", "↙", "←", "↖")
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


@dataclass
class _TuiState:
    """All mutable UI state. Owned by the main (UI) thread."""

    fires: list[dict] = field(default_factory=list)
    distances: dict[int, float] = field(default_factory=dict)
    threat_scores: dict[int, float] = field(default_factory=dict)
    threat_factors: dict[int, dict] = field(default_factory=dict)
    acreage_history: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    distance_history: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    wind: dict[int, dict] = field(default_factory=dict)
    grown_fire_ids: set[int] = field(default_factory=set)

    visible_fires: list[dict] = field(default_factory=list)
    reports_cache: dict[int, list[dict]] = field(default_factory=dict)
    radio_cache: dict[int, list[dict]] = field(default_factory=dict)
    cameras_cache: dict[int, list[dict]] = field(default_factory=dict)
    fps_cache: dict[int, list[dict]] = field(default_factory=dict)
    image_cache: dict[str, bytes] = field(default_factory=dict)
    chip_cache: dict[int, list[dict]] = field(default_factory=dict)
    aircraft_catalog: list[dict] = field(default_factory=list)

    selected_idx: int = 0
    # Stable selection anchor: id of the selected fire. _recompute_visible
    # re-derives selected_idx from this after every re-filter/re-sort so a
    # refresh keeps the same FIRE selected, not the same row number.
    selected_fire_id: int | None = None
    list_scroll: int = 0
    detail_scroll: int = 0
    sort_key: str = "threat"
    sort_reverse: bool = False
    filter_text: str = ""
    filter_active: bool = False
    filter_buffer: str = ""
    filter_original: str = ""   # snapshot at `/` so ESC can cancel
    cmd_active: bool = False
    cmd_buffer: str = ""
    focus: str = _FOCUS_LIST
    active_tab: str = "updates"
    list_compact: bool = False
    image_size: str = "med"   # one of "small" / "med" / "large"
    mouse_wheel_invert: bool = False
    mouse_debug: bool = False
    last_mouse_bstate: int = 0   # for debug: shown in status when invert toggles

    last_refresh_ts: float = 0.0
    refresh_in_flight: bool = False
    status_msg: str = ""
    status_msg_ts: float = 0.0
    status_is_error: bool = False
    # Escape strings (bell / OSC notifications) queued for stdout; the
    # app loop flushes these right after curses.doupdate() so raw
    # writes can't race the curses frame.
    pending_stdout: list[str] = field(default_factory=list)
    pending_requests: set[tuple] = field(default_factory=set)
    loading_reports_for: int | None = None

    types: tuple[str, ...] = GEO_EVENT_TYPES
    near: tuple[float, float] | None = None
    near_source: str = ""
    within_km: float = 250.0
    auto_refresh: int = 0
    # Threat scoring model: "v1" = legacy size+wind-gain multiplicative,
    # "v2" = Candidate A ISI-anchored physics-informed (see threat.py).
    # Default v1 preserves existing behaviour; switch with `:threat-model v2`
    # or via the LIBWATCHDUTY_THREAT_MODEL env var at startup.
    threat_model: str = "v1"

    quit: bool = False
    last_g: float = 0.0
    last_left: float = 0.0
    live_mode: bool = False
    last_live_poll_ts: float = 0.0
    flash_report_ids: set[int] = field(default_factory=set)
    flash_report_ts: float = 0.0

    image_show_for: int | None = None
    image_show_url: str | None = None
    pending_mapscii: tuple[float, float] | None = None
    mapscii_embed: Any = None        # _MapsciiEmbed | None
    mapscii_rect: tuple = ()         # (y0, x0, h, w) where we paint it
    last_scroll_change_ts: float = 0.0
    last_known_detail_scroll: int = 0
    last_prefetch_for: int | None = None
    bulk_prefetched: bool = False
    header_image_url: str | None = None
    header_image_last_fire: int | None = None
    header_image_last_paint_ts: float = 0.0
    header_image_enabled: bool = True
    update_image_slots: list = field(default_factory=list)
    update_image_pending: list = field(default_factory=list)
    update_image_painted: set = field(default_factory=set)

    last_drawn_fire_id: int | None = None
    last_drawn_tab: str = ""
    last_drawn_detail_scroll: int = 0
    tab_rects: list = field(default_factory=list)
    detail_scroll_max: int = 0
    list_scroll_max: int = 0
