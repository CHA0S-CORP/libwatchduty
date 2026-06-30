# Contributing to libwatchduty

Thanks for your interest in contributing! This project is an unofficial Python
client for [app.watchduty.org](https://app.watchduty.org) fire/incident data.
Bug reports, feature requests, and pull requests are all welcome.

## Getting set up

### 1. Clone the repository

```bash
git clone https://github.com/chaos-corp/libwatchduty.git
cd libwatchduty
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate         # macOS / Linux
# .venv\Scripts\activate          # Windows PowerShell
```

Python 3.10+ is recommended (CI tests 3.10, 3.11, 3.12, 3.13).

### 3. Install the package in editable mode with dev extras

```bash
pip install --upgrade pip
pip install -e ".[test,tui]"
```

The `test` extra installs `pytest` and any other test dependencies.
The `tui` extra installs `pyte`, used by the embedded mapscii map view.

### 4. Run the tests

```bash
pytest tests/
```

For verbose output and a summary of skips/failures:

```bash
pytest tests/ -ra
```

### 5. (Optional) Run the linter

If a ruff config is present:

```bash
pip install ruff
ruff check src tests
```

## Making changes

1. **Create a branch** off `main`:

   ```bash
   git checkout -b my-feature
   ```

2. **Make focused commits** with clear messages. Prefer small, reviewable
   commits over one giant change.

3. **Add or update tests** for any behavior change. New features should
   come with tests that demonstrate them.

4. **Update the changelog.** Add an entry under `[Unreleased]` in
   `CHANGELOG.md` describing what changed.

5. **Run the test suite locally** before opening a PR:

   ```bash
   pytest tests/
   ```

## Opening a pull request

1. Push your branch:

   ```bash
   git push -u origin my-feature
   ```

2. Open a PR against `main` on
   [github.com/chaos-corp/libwatchduty](https://github.com/chaos-corp/libwatchduty).

3. Fill in the PR template — describe the change, link related issues,
   and include a test plan.

4. CI will run the test matrix (Python 3.10-3.13 on Ubuntu and macOS).
   Please make sure it passes; push fixups as needed.

## Reporting bugs

Open an issue with:

- What you were trying to do
- What happened instead
- A minimal reproduction (code snippet, command, sample data if possible)
- Your OS and Python version
- The installed version of `libwatchduty`

## Code style

- Follow existing patterns in `src/libwatchduty/`.
- Keep public API surface small and documented via docstrings.
- Avoid adding heavy dependencies; prefer the standard library when reasonable.

## License

By contributing you agree that your contributions will be licensed under
the MIT License, the same license that covers the project.
