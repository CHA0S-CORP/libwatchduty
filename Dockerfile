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
# Run a one-shot CLI subcommand:
#   docker run --rm libwatchduty:latest fires --active
#   docker run --rm libwatchduty:latest event 105316

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# Node.js — the bundled mapscii (shipped as wheel data under
# share/libwatchduty/vendor/mapscii/) is a Node app and needs `node`
# on $PATH. Slim Debian's Node is fine for mapscii's runtime needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       nodejs \
       ca-certificates \
       tini \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Pin to a published version so the image is reproducible.
# Bump with each release.
ARG LIBWATCHDUTY_VERSION=0.1.3
RUN pip install --no-cache-dir "libwatchduty[tui]==${LIBWATCHDUTY_VERSION}"

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
