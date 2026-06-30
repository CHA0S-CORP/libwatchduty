# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Deprecated

### Removed

### Fixed

### Security

## [0.1.2] - 2026-06-30

### Added

- New `threat` module and opt-in **v2 scoring** model based on the
  Canadian Forest Fire Weather Index system (Van Wagner ISI). Select
  with `:threat-model v2` in the TUI or `WATCHDUTY_THREAT_MODEL=v2`
  in the environment. See
  [`docs/THREAT_SCORING.md`](docs/THREAT_SCORING.md) for the formula,
  comparison table, and roadmap.
- Six annotated, runnable example programs under `examples/`
  (`list_fires`, `nearest_fires`, `live_watcher`, `fire_bundle`,
  `radio_feeds`, `camera_capture`), indexed by
  [`docs/EXAMPLES.md`](docs/EXAMPLES.md).
- [`docs/LOCATION.md`](docs/LOCATION.md) — full documentation of
  `--near auto`: CoreLocationCLI on macOS, IP-geolocation fallbacks,
  privacy notes, programmatic `detect_location()` usage.
- Mouse-wheel **zoom on the embedded mapscii** — scrolling up over
  the Map-tab rect sends `a` (zoom in), scrolling down sends `z`
  (zoom out). Honors `:mouse-invert`.

### Changed

- README **Threat scoring** section now leads with a WIP/disclaimer
  admonition noting that v1 is intentionally naive and pointing at
  the v2 docs.
- `CONTRIBUTING.md` tightened: develop → patch flow → code style →
  release flow, all in under 90 lines.
- `docs/QUICKSTART.md` §6 cross-links the new `LOCATION.md` and
  `EXAMPLES.md`.

## [0.1.1] - 2026-06-30

### Added

- README hero screenshots: `docs/screenshots/update-view.jpg` and
  `docs/screenshots/map-view.jpg`.
- `docs/QUICKSTART.md` now opens with a **virtualenv** section
  (venv / `pipx` / `uv tool`) so users hit PEP 668-blocked system
  Pythons less often.
- Dedicated **Install the mapscii Node binary** subsection in the
  Quick Start covering the bundled wheel data, the
  `watchduty-install-mapscii` fallback, and per-platform Node install
  hints.

### Changed

- Quick Start prefers `pip install 'libwatchduty[tui]'` quoting style
  so zsh doesn't mis-parse the extras bracket.
- README links to the new Quick Start; org references updated to
  `CHA0S-CORP`.

## [0.1.0] - 2026-06-29

### Added

- Initial release of `libwatchduty`, an unofficial Python client for
  app.watchduty.org fire/incident data.
- `watchduty` CLI entry point.
- `watchduty-install-mapscii` helper for the optional TUI map.
- Optional `tui` extra (`pyte`) for the embedded VT100 map view.

[Unreleased]: https://github.com/CHA0S-CORP/libwatchduty/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/CHA0S-CORP/libwatchduty/releases/tag/v0.1.0
