"""Inline-image protocol detection: env sniffing + WATCHDUTY_INLINE_IMAGES override."""

import io

import pytest

from libwatchduty import images


class _FakeTTY(io.StringIO):
    def __init__(self, tty: bool = True):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# Env vars that steer detection; cleared before every case so the host
# terminal running the tests can't leak in.
_ENV_KEYS = (
    "WATCHDUTY_INLINE_IMAGES",
    "TERM",
    "TERM_PROGRAM",
    "LC_TERMINAL",
    "KITTY_WINDOW_ID",
    "NO_COLOR",
)


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_no_tty_means_no_images(clean_env):
    clean_env.setenv("TERM_PROGRAM", "iTerm.app")
    assert images.supports_iterm2(_FakeTTY(tty=False)) is False
    assert images.supports_inline_images(_FakeTTY(tty=False)) is False


def test_vscode_detected_as_iterm2(clean_env):
    clean_env.setenv("TERM_PROGRAM", "vscode")
    tty = _FakeTTY()
    assert images.supports_iterm2(tty) is True
    assert images.supports_kitty(tty) is False
    # render_inline picks the iTerm2 escape (OSC 1337 File=).
    esc = images.render_inline(b"\x89PNG\r\n", stream=tty)
    assert esc.startswith("\x1b]1337;File=")


def test_iterm_and_kitty_native_detection(clean_env):
    tty = _FakeTTY()
    clean_env.setenv("TERM_PROGRAM", "iTerm.app")
    assert images.supports_iterm2(tty) is True
    clean_env.setenv("TERM_PROGRAM", "ghostty")
    assert images.supports_kitty(tty) is True
    clean_env.delenv("TERM_PROGRAM", raising=False)
    clean_env.setenv("TERM", "xterm-kitty")
    assert images.supports_kitty(tty) is True


def test_plain_terminal_has_no_inline_images(clean_env):
    clean_env.setenv("TERM", "xterm-256color")
    assert images.supports_inline_images(_FakeTTY()) is False


def test_force_iterm2_override(clean_env):
    # No terminal hints at all — the override alone enables it. This is the
    # Docker/VS Code case where TERM_PROGRAM never reaches the container.
    clean_env.setenv("WATCHDUTY_INLINE_IMAGES", "iterm2")
    tty = _FakeTTY()
    assert images.supports_iterm2(tty) is True
    assert images.supports_kitty(tty) is False
    assert images.supports_inline_images(tty) is True


def test_force_kitty_override(clean_env):
    clean_env.setenv("WATCHDUTY_INLINE_IMAGES", "kitty")
    tty = _FakeTTY()
    assert images.supports_kitty(tty) is True
    assert images.supports_iterm2(tty) is False


def test_force_off_override_beats_detection(clean_env):
    clean_env.setenv("TERM_PROGRAM", "iTerm.app")
    clean_env.setenv("WATCHDUTY_INLINE_IMAGES", "off")
    tty = _FakeTTY()
    assert images.supports_iterm2(tty) is False
    assert images.supports_inline_images(tty) is False


def test_override_still_requires_a_tty(clean_env):
    clean_env.setenv("WATCHDUTY_INLINE_IMAGES", "iterm2")
    assert images.supports_iterm2(_FakeTTY(tty=False)) is False


def test_override_beats_no_color(clean_env):
    clean_env.setenv("NO_COLOR", "1")
    clean_env.setenv("WATCHDUTY_INLINE_IMAGES", "iterm2")
    assert images.supports_iterm2(_FakeTTY()) is True
