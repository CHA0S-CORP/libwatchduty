"""One-shot installer for the optional ``mapscii`` map viewer.

mapscii is a Node.js application (https://github.com/rastapasta/mapscii); it
cannot live inside a Python wheel directly. This module ships as the
``watchduty-install-mapscii`` console script so users can run the npm install
without leaving their pip-based workflow.

Exits non-zero on any failure so CI/automation can detect it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


_INSTALL_HINTS = """\
mapscii requires Node.js (>= 14) and npm on your PATH.

Install Node.js:
  macOS:        brew install node
  Debian/Ubuntu: sudo apt-get install -y nodejs npm
  Windows:      https://nodejs.org/en/download/
"""


def main() -> int:
    """Run ``npm install -g mapscii``; return a shell exit code.

    No-op when the in-repo `vendor/mapscii` checkout is present (preferred
    by the TUI) or when mapscii is already on $PATH.
    """
    # Prefer the in-repo vendored copy when present.
    try:
        from .tui import _bundled_mapscii  # type: ignore
        if _bundled_mapscii():
            print("bundled mapscii detected — nothing to do.")
            return 0
    except Exception:
        pass
    if shutil.which("mapscii"):
        print("mapscii is already on your PATH — nothing to do.")
        return 0
    npm = shutil.which("npm")
    if not npm:
        sys.stderr.write(_INSTALL_HINTS)
        return 2
    print(f"running: {npm} install -g mapscii")
    try:
        r = subprocess.run([npm, "install", "-g", "mapscii"], check=False)
    except OSError as e:
        sys.stderr.write(f"failed to launch npm: {e}\n")
        return 3
    if r.returncode != 0:
        sys.stderr.write(
            "npm install -g mapscii failed. "
            "You may need to rerun with sudo, "
            "or set a user-writable npm prefix (`npm config set prefix ~/.npm-global`).\n"
        )
        return r.returncode
    if not shutil.which("mapscii"):
        sys.stderr.write(
            "mapscii installed but is not on your PATH. "
            "Check `npm bin -g` and add that directory to PATH.\n"
        )
        return 4
    print("mapscii installed. Press `m` in `watchduty tui` to launch it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
