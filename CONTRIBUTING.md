# Contributing to libwatchduty

Unofficial Python client + terminal dashboard for
[app.watchduty.org](https://app.watchduty.org). Bug reports, feature
requests, and pull requests are welcome.

## Develop

```bash
git clone https://github.com/CHA0S-CORP/libwatchduty.git
cd libwatchduty

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e '.[test,tui]'

pytest tests/                          # 36 tests, all pure-Python
pytest tests/ -ra --tb=short           # verbose with skip summary
```

Python ≥3.10 (CI runs 3.10–3.13 on Ubuntu and macOS). The `test`
extra adds `pytest` + `responses`; `tui` adds `pyte` for the
embedded mapscii VT.

### Optional: lint

```bash
pip install ruff
ruff check src tests
```

### Optional: re-capture screenshots

```bash
python scripts/capture_screenshots.py     # writes docs/screenshots/
```

## Patch flow

1. Branch off `main`. Small, focused commits beat one giant change.
2. Add or update tests for any behavior change. Run `pytest tests/`
   before pushing.
3. Drop an entry under `[Unreleased]` in `CHANGELOG.md`.
4. Push, open a PR against `main`, fill in the template. CI runs the
   matrix; please keep it green.

## Code style

- Match the conventions already in `src/libwatchduty/`.
- Public API is small and docstring-documented. Add docstrings to new
  public surface.
- Stdlib first — adding a runtime dependency needs a justification in
  the PR description.
- Keep network-touching code in `client.py` (or the relevant module),
  off the UI thread; the TUI's `_TuiState` is the single source of
  truth.

## Reporting bugs

Open an [issue](https://github.com/CHA0S-CORP/libwatchduty/issues)
with:

- What you were trying to do.
- What happened instead (error trace, screenshot, terminal recording).
- Minimal reproduction (a `WatchDutyClient` call, a key sequence in
  the TUI).
- Your OS, Python version, and `libwatchduty` version
  (`pip show libwatchduty`).
- If TUI: your `$TERM` and terminal app (iTerm2 / ghostty / kitty /
  Terminal.app / tmux session inside one of those).

## Release flow

1. Bump `version` in `pyproject.toml`.
2. Move items from `[Unreleased]` into a new `[X.Y.Z]` section in
   `CHANGELOG.md`.
3. Commit, tag `vX.Y.Z`, push the tag — `.github/workflows/publish.yml`
   builds and publishes to PyPI via OIDC Trusted Publisher.
4. The full one-time setup of the publisher is captured in
   [`docs/CHAOS_CORP_RELEASE.md`](docs/CHAOS_CORP_RELEASE.md).

## License

By contributing you agree that your contributions will be licensed
under the MIT License, the same license that covers the project.
