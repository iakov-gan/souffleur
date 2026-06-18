"""Terminal color helpers for Souffleur's console output.

Each speaker is given a *stable* color derived from a hash of their name, so the
same person is always shown in the same color across a session (and across
restarts). System/status messages use a single muted color, and errors red.

Coloring is automatic: it is enabled only when the target stream is a real
terminal, and is suppressed when the ``NO_COLOR`` environment variable is set or
``TERM=dumb`` (so piping/redirecting to a file stays clean). On Windows the
virtual-terminal (ANSI) mode is enabled on import.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

RESET = "\033[0m"
_SYSTEM = "\033[90m"      # bright black / gray
_ERROR = "\033[91m"       # bright red
_DIM = "\033[2m"          # faint (timestamps)

# A curated set of ANSI-256 colors that read well on both dark and light
# backgrounds. Pure grays/whites are deliberately excluded — they are reserved
# for system messages — as are the very dark shades that vanish on dark themes.
_PALETTE = [
    39, 208, 41, 213, 220, 51, 203, 141, 118, 45, 214, 99,
    84, 199, 75, 215, 170, 80, 156, 117, 209, 113, 147, 178,
    49, 218, 159, 111, 205, 150, 186, 81,
]

_CAPTION_RE = re.compile(r"<([^<>]*?):>")
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _enable_windows_vt() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ENABLE_VT = 0x0004
        for std in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT)
    except Exception:
        pass


_enable_windows_vt()


def _supports(stream) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


# Whether color is on for each standard stream (computed once at import).
COLOR_STDOUT = _supports(sys.stdout)
COLOR_STDERR = _supports(sys.stderr)


def _code_for(name: str) -> str:
    """Stable ANSI-256 color for a speaker name (case/space-insensitive)."""
    digest = hashlib.md5(name.strip().lower().encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "big") % len(_PALETTE)
    return f"\033[38;5;{_PALETTE[idx]}m"


def visible_len(text: str) -> int:
    """Length of ``text`` ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", text))


def speaker(name: str, on: bool = True) -> str:
    """Return ``name`` wrapped in its stable color."""
    if not (on and name):
        return name
    return f"{_code_for(name)}{name}{RESET}"


def speaker_prefix(text: str, name: str, on: bool = True) -> str:
    """Color a leading ``name`` in ``text`` (used for live, truncated lines)."""
    if on and name and text.startswith(name):
        return f"{_code_for(name)}{name}{RESET}{text[len(name):]}"
    return text


def caption(text: str, on: bool = True) -> str:
    """Color every ``<Name:>`` speaker tag found in ``text`` by speaker."""
    if not on:
        return text
    return _CAPTION_RE.sub(
        lambda m: f"{_code_for(m.group(1))}{m.group(0)}{RESET}", text
    )


def system(text: str, on: bool = True) -> str:
    return f"{_SYSTEM}{text}{RESET}" if on else text


def error(text: str, on: bool = True) -> str:
    return f"{_ERROR}{text}{RESET}" if on else text


def dim(text: str, on: bool = True) -> str:
    return f"{_DIM}{text}{RESET}" if on else text
