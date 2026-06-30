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
