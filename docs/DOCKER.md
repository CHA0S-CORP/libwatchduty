# Running libwatchduty in Docker

Self-contained image — pinned `libwatchduty[tui]` from PyPI, Node.js
for the bundled mapscii, a non-root user, `tini` as PID 1.

---

## Pull or build

```bash
# Published — built by .github/workflows/docker.yml on every push to
# main and every v* tag. linux/amd64 + linux/arm64.
docker pull ghcr.io/cha0s-corp/libwatchduty:latest
docker pull ghcr.io/cha0s-corp/libwatchduty:0.1.3

# Or build locally from the repo root
docker build -t libwatchduty:0.1.3 .
```

The `LIBWATCHDUTY_VERSION` build arg pins the PyPI version installed
inside the image — bump it with each release:

```bash
docker build --build-arg LIBWATCHDUTY_VERSION=0.1.3 -t libwatchduty:0.1.3 .
```

---

## Run the TUI

The TUI needs an interactive terminal, so use `-it` (or `--tty
--interactive`). It honours `$TERM` and falls back to `xterm-256color`
in the image.

```bash
docker run --rm -it libwatchduty:0.1.3

# With your home coordinates baked in (skip --near auto's IP lookup):
docker run --rm -it \
  -e WATCHDUTY_HOME=33.92,-117.24 \
  libwatchduty:0.1.3

# Different refresh interval:
docker run --rm -it libwatchduty:0.1.3 \
  tui --near 37.77,-122.42 --within 100 --refresh 90
```

### True-color + clipboard

Most modern terminals (iTerm2, ghostty, kitty, WezTerm) Just Work
because the container inherits the host TTY. If colors look wrong:

```bash
# Force a known TERM:
docker run --rm -it -e TERM=xterm-256color libwatchduty:0.1.3

# kitty graphics protocol for the inline camera frames:
docker run --rm -it -e TERM=xterm-kitty libwatchduty:0.1.3
```

### mapscii

mapscii ships inside the wheel as shared data and is auto-detected by
the TUI's resolver. If the bundled copy ever breaks (Node version
mismatch, etc.), drop into the container and rebuild it:

```bash
docker run --rm -it --entrypoint sh libwatchduty:0.1.3 \
  -c 'watchduty-install-mapscii'
```

---

## Run CLI subcommands

No TTY needed for plain CLI calls — the image's `ENTRYPOINT` is
`watchduty`, so just pass arguments:

```bash
docker run --rm libwatchduty:0.1.3 fires --active
docker run --rm libwatchduty:0.1.3 event 105316
docker run --rm libwatchduty:0.1.3 reports 105316
docker run --rm libwatchduty:0.1.3 radio --latlng 33.92,-117.24
```

Pipe JSON out for processing:

```bash
docker run --rm libwatchduty:0.1.3 fires --active --json \
  | jq '.[] | select(.data.acreage > 1000)'
```

---

## Capture stills + bundles to your host

Mount a host directory at `/data` and point the CLI at it:

```bash
docker run --rm \
  -v "$PWD/captures:/data" \
  --workdir /data \
  libwatchduty:0.1.3 \
  stills capture 105316 --out /data/cam.jpg

docker run --rm \
  -v "$PWD/captures:/data" \
  --workdir /data \
  libwatchduty:0.1.3 \
  bundle 105316 > "$PWD/captures/105316.json"
```

The container user is `watchduty` (uid 10001). If your host dir is
owned by a different uid, either `chown` ahead of time or pass
`--user "$(id -u):$(id -g)"`.

---

## Authenticated API token

```bash
docker run --rm -it \
  -e WATCHDUTY_TOKEN="paste-your-token-here" \
  libwatchduty:0.1.3 places
```

Or stash it in a `.env` file (NEVER commit it):

```bash
cat > .env <<'EOF'
WATCHDUTY_TOKEN=…
WATCHDUTY_HOME=33.92,-117.24
EOF

docker run --rm -it --env-file .env libwatchduty:0.1.3
```

---

## docker-compose (optional)

```yaml
services:
  watchduty:
    image: libwatchduty:0.1.3
    stdin_open: true
    tty: true
    environment:
      WATCHDUTY_HOME: "33.92,-117.24"
      TERM: xterm-256color
    volumes:
      - ./captures:/data
```

```bash
docker compose run --rm watchduty
```

---

## What's in the image

```
$ docker image inspect libwatchduty:0.1.3 --format='{{.Config.Cmd}} :: {{.Size}} bytes'
```

| Layer | Why |
|---|---|
| `python:3.12-slim` | Tiny CPython base (~ 50 MB) |
| `nodejs` (Debian) | Runs the bundled mapscii script |
| `ca-certificates` | TLS for `api.watchduty.org` |
| `tini` | PID 1, reaps Node children cleanly on exit |
| `libwatchduty[tui]` from PyPI | The package + pyte for the inline map |
| `watchduty` user (uid 10001) | Non-root runtime |

No source code is COPY'd in; the wheel is the single shipped artefact.
Whole image is well under 200 MB.

---

## Troubleshooting

| Symptom | Try |
|---|---|
| TUI shows "terminal too small" | Resize host terminal, or pass `-e LINES=40 -e COLUMNS=160` |
| Mapscii tab shows quadrant fallback | `--entrypoint sh` and run `watchduty-install-mapscii` |
| Colors look 8-bit | Force `-e TERM=xterm-256color` (or `xterm-kitty` for graphics) |
| Auto-locate fails | Container has no host geo APIs — pass `WATCHDUTY_HOME=lat,lng` |
| Mouse wheel doesn't work | Some terminal multiplexers swallow mouse over Docker; try without `tmux`/`screen` |
| Container exits immediately | You forgot `-it` for the TUI |
