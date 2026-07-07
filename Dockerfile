# libwatchduty container — slim Python base + Node (for the bundled
# mapscii binary) + pip install from PyPI. Default entrypoint launches
# the TUI; override the CMD for plain CLI subcommands.
#
# Build:
#   docker build -t libwatchduty:latest .
#
# Run the TUI (needs an interactive TTY):
#   docker run --rm -it \
#     -e WATCHDUTY_HOME=33.92,-117.24 \
#     libwatchduty:latest
#
# Inline camera images: the TUI auto-detects kitty/ghostty/iTerm2/VS Code,
# but `docker run` does not forward the host's $TERM_PROGRAM into the
# container, so detection can't see it. Force the protocol explicitly.
# In the VS Code integrated terminal (enable terminal.integrated.enableImages),
# it speaks the iTerm2 protocol:
#   docker run --rm -it \
#     -e WATCHDUTY_INLINE_IMAGES=iterm2 \
#     -e WATCHDUTY_HOME=33.92,-117.24 \
#     libwatchduty:latest
# Use WATCHDUTY_INLINE_IMAGES=kitty for a kitty/ghostty host terminal.
#
# Run a one-shot CLI subcommand:
#   docker run --rm libwatchduty:latest fires --active
#   docker run --rm libwatchduty:latest event 105316

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# Node.js — the bundled mapscii (shipped as wheel data under
# share/libwatchduty/vendor/mapscii/) is a Node app and needs `node`
# on $PATH. npm is needed at build time to repopulate mapscii's
# dependencies (see the mapscii RUN below); it is left installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       nodejs \
       npm \
       ca-certificates \
       tini \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Pin to a published version so the image is reproducible.
# Bump with each release.
ARG LIBWATCHDUTY_VERSION=0.1.5
RUN pip install --no-cache-dir "libwatchduty[tui]==${LIBWATCHDUTY_VERSION}"

# Make the bundled mapscii actually runnable. The wheel ships mapscii's
# node_modules as static data, but the packaging flattens npm's bin
# symlinks and drops dependency files (e.g. node-fetch/lib/), so the
# copy pip installs cannot launch. Reinstall the dependency tree from
# the pinned lockfile, preserving the libwatchduty patch to main.js
# (env-var recentering via MAPSCII_LAT/LNG/ZOOM that upstream lacks).
RUN VENDOR="$(python -c 'import sys,os; print(os.path.join(sys.prefix, "share", "libwatchduty", "vendor", "mapscii"))')" \
    && cp "${VENDOR}/node_modules/mapscii/main.js" /tmp/mapscii-main.js \
    && cd "${VENDOR}" \
    && npm ci --omit=dev --no-audit --no-fund \
    && cp /tmp/mapscii-main.js "${VENDOR}/node_modules/mapscii/main.js" \
    && rm -f /tmp/mapscii-main.js \
    && npm cache clean --force

# A non-root user for safety; the TUI doesn't need root.
RUN useradd --create-home --uid 10001 watchduty
USER watchduty
WORKDIR /home/watchduty

ENV TERM=xterm-256color \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# tini reaps Node subprocesses cleanly when the TUI exits.
ENTRYPOINT ["/usr/bin/tini", "--", "watchduty"]
CMD ["tui", "--near", "auto", "--within", "250", "--refresh", "60"]

# Standard OCI labels for the registry view.
LABEL org.opencontainers.image.title="libwatchduty" \
      org.opencontainers.image.description="Unofficial Watch Duty wildfire client + threat-ranked terminal dashboard" \
      org.opencontainers.image.source="https://github.com/CHA0S-CORP/libwatchduty" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.url="https://pypi.org/project/libwatchduty/"
