"""
daemon.py — the "souffleur" background daemon.

Reads the live Microsoft Teams transcript in the background and, on ONE global
hotkey, pastes the latest transcript (delta since the previous send, or the
whole thing on the first fire) into the *current* Clawpilot / Microsoft Scout
chat and clicks Send. You prime the chat once with your persona/instruction;
each hotkey press feeds it fresh context for a live answer.

Pieces:
  * teams_ui.TranscriptReader — background Teams caption capture (self-healing).
  * scout.ScoutWriter        — clipboard-paste + Send into Clawpilot via UIA.
  * HotkeyMonitor            — the single global trigger (GetAsyncKeyState poll).

Design notes:
  * The hotkey is detected by polling the real-time keyboard state with
    GetAsyncKeyState, NOT a low-level keyboard hook. A hook callback must return
    within Windows' ~300 ms LowLevelHooksTimeout or the OS silently drops the
    keystroke; under GIL contention from the UIA reader thread that timeout is
    easily blown, causing missed presses. Polling samples the physical key state
    and is immune to hook timeouts and GIL timing.
  * The hotkey thread does NO UI work — it only signals the main thread, which
    performs every Clawpilot UIA action. This serializes UIA.
  * The main loop doubles as a watchdog: between hotkey fires it checks that
    Clawpilot is still alive and relaunches it if not.

Usage:
    souffleur run                       # preferred entry point
    python -m souffleur.daemon          # uses ./config.toml (auto-created)
    python -m souffleur.daemon --config x.toml
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

from .teams_ui import TranscriptReader
from .scout import ScoutError, ScoutWriter
from . import colors

# config.toml in the current working directory (auto-created on first run).
DEFAULT_CONFIG_PATH = Path("config.toml")

DEFAULT_CONFIG_TEXT = """\
# souffleur prompter configuration.
# Auto-created with these defaults if missing. Edit and restart the daemon.

[hotkey]
combo = "win+ctrl+alt"
# How close together (seconds) the chord keys must be pressed. A short sliding
# window so a one-handed "rolling" press (keys landing a few ms apart) still
# fires reliably without needing all keys down in the exact same instant.
window = 0.25

[clawpilot]
exe = "C:/Program Files (x86)/Clawpilot/Clawpilot.exe"
window_title = "Clawpilot"
foreground_on_start = true

[send]
mode = "delta"
max_chars = 12000
include_live = true
restore_clipboard = true
wait_for_idle = true
idle_timeout = 90.0
retry_interval = 1.0
template = "Here is a transcript of the meeting (or follow-up):\\n'''\\n{payload}\\n'''\\nFind the latest question(s) and answer as an expert."

[capture]
interval = 0.5

[watchdog]
interval = 5.0
"""

TRIM_MARKER = "...[earlier transcript trimmed]...\n"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        _log(f"created default config at {path}")
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    return cfg


def _cfg(cfg: dict, section: str, key: str, default):
    return cfg.get(section, {}).get(key, default)


# --------------------------------------------------------------------------- #
# hotkey: parse a combo to Windows virtual-key codes and poll their live state.
# --------------------------------------------------------------------------- #
_VK_NAMED = {
    "ctrl": [0x11], "control": [0x11],
    "alt": [0x12], "option": [0x12],
    "shift": [0x10],
    # Either Windows key satisfies "win".
    "win": [0x5B, 0x5C], "windows": [0x5B, 0x5C],
    "super": [0x5B, 0x5C], "meta": [0x5B, 0x5C], "cmd": [0x5B, 0x5C],
    "space": [0x20], "enter": [0x0D], "return": [0x0D],
    "tab": [0x09], "esc": [0x1B], "escape": [0x1B], "backspace": [0x08],
}


def parse_combo(combo: str) -> list[list[int]]:
    """Parse "win+ctrl+alt" (or "ctrl+f8", "ctrl+shift+z") into a list of
    virtual-key groups. Each group is a list of acceptable VK codes (more than
    one when left/right variants both qualify). The combo fires when at least
    one VK in every group is held down.
    """
    groups: list[list[int]] = []
    for raw in combo.split("+"):
        tok = raw.strip().lower()
        if not tok:
            continue
        # strip pynput-style <...>
        if len(tok) > 2 and tok[0] == "<" and tok[-1] == ">":
            tok = tok[1:-1]
        if tok in _VK_NAMED:
            groups.append(list(_VK_NAMED[tok]))
        elif len(tok) == 1 and (tok.isalnum()):
            groups.append([ord(tok.upper())])
        elif tok.startswith("f") and tok[1:].isdigit() and 1 <= int(tok[1:]) <= 24:
            groups.append([0x70 + int(tok[1:]) - 1])  # VK_F1 = 0x70
        else:
            raise ValueError(f"unrecognized hotkey token: {raw!r}")
    if not groups:
        raise ValueError("empty hotkey combo")
    return groups


def pretty_combo(combo: str) -> str:
    """Human-readable label, e.g. 'Win+Ctrl+Alt'."""
    nice = {"win": "Win", "windows": "Win", "super": "Win", "meta": "Win",
            "cmd": "Win", "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt",
            "option": "Alt", "shift": "Shift", "space": "Space"}
    parts = []
    for raw in combo.split("+"):
        t = raw.strip().lower().strip("<>")
        if not t:
            continue
        parts.append(nice.get(t, t.upper() if len(t) == 1 else t.capitalize()))
    return "+".join(parts)


class HotkeyMonitor:
    """Polls GetAsyncKeyState for a key combo and fires once per chord-hold.

    Immune to low-level-hook timeouts and GIL contention: it samples the
    physical keyboard state on a short interval. Rather than demanding that
    every key in the chord be down in one single sample (ergonomically hard for
    a 3-modifier combo pressed one-handed, where fingers roll on in sequence),
    it fires when every key has been seen down within a short sliding window
    (``window`` seconds). It re-arms only after a sample shows the whole chord
    released, so one hold = one fire.
    """

    def __init__(self, combo: str, on_fire, *, poll: float = 0.025,
                 window: float = 0.25):
        self.groups = parse_combo(combo)
        self.on_fire = on_fire
        self.poll = poll
        self.window = window
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._user32 = ctypes.windll.user32

    def _down(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _group_down(self, group) -> bool:
        return any(self._down(vk) for vk in group)

    def start(self) -> "HotkeyMonitor":
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="HotkeyMonitor", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        ngroups = len(self.groups)
        last_down = [0.0] * ngroups  # perf_counter when each group was last down
        prev_chord = False
        while not self._stop.is_set():
            now = time.perf_counter()
            for i, group in enumerate(self.groups):
                if self._group_down(group):
                    last_down[i] = now
            # chord present if every group was seen down within the window
            chord = all((now - t) <= self.window for t in last_down)
            if chord and not prev_chord:  # rising edge of the windowed chord
                try:
                    self.on_fire()
                except Exception:
                    pass
            prev_chord = chord
            self._stop.wait(self.poll)


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str) -> None:
    CONSOLE.line(colors.system(f"[{_ts()}] {msg}", colors.COLOR_STDOUT))


def _err(msg: str) -> None:
    CONSOLE.line(colors.error(f"[{_ts()}] !! {msg}", colors.COLOR_STDERR), err=True)


# --------------------------------------------------------------------------- #
# console — thread-safe output that keeps a single, in-place "live" line so the
# growing (not-yet-finalized) caption paragraph is visible in real time without
# scrolling the finalized transcript off screen.
# --------------------------------------------------------------------------- #
class Console:
    def __init__(self):
        self._lock = threading.Lock()
        self._live_len = 0  # width of the live line currently drawn (0 = none)

    def _clear_live(self) -> None:
        if self._live_len:
            sys.stdout.write("\r" + " " * self._live_len + "\r")
            self._live_len = 0

    def line(self, text: str, err: bool = False) -> None:
        """Print a permanent line, clearing any live line first."""
        with self._lock:
            self._clear_live()
            stream = sys.stderr if err else sys.stdout
            stream.write(text + "\n")
            stream.flush()

    def live(self, text: str | None) -> None:
        """Redraw the in-place live line (overwrites the previous one)."""
        with self._lock:
            if not text:
                self._clear_live()
                sys.stdout.flush()
                return
            shown = f"  … {text}"
            try:
                cols = (__import__("shutil").get_terminal_size((100, 20)).columns)
            except Exception:
                cols = 100
            if len(shown) > cols - 1:
                shown = shown[: cols - 2] + "…"
            pad = max(0, self._live_len - len(shown))
            sys.stdout.write("\r" + colors.caption(shown, colors.COLOR_STDOUT)
                             + " " * pad)
            sys.stdout.flush()
            self._live_len = len(shown)


CONSOLE = Console()


# --------------------------------------------------------------------------- #
# prompter
# --------------------------------------------------------------------------- #
class Prompter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = str(_cfg(cfg, "send", "mode", "delta")).lower()
        self.max_chars = int(_cfg(cfg, "send", "max_chars", 12000))
        self.include_live = bool(_cfg(cfg, "send", "include_live", True))
        self.template = str(_cfg(cfg, "send", "template",
                                 "Here is a transcript of the meeting (or follow-up):\n"
                                 "'''\n{payload}\n'''\n"
                                 "Find the latest question(s) and answer as an expert."))
        # When Clawpilot is mid-answer, hold the request and dispatch it the
        # moment it goes idle instead of dropping the press.
        self.wait_for_idle = bool(_cfg(cfg, "send", "wait_for_idle", True))
        self.idle_timeout = float(_cfg(cfg, "send", "idle_timeout", 90.0))
        self.retry_interval = float(_cfg(cfg, "send", "retry_interval", 1.0))

        self.reader = TranscriptReader(
            interval=float(_cfg(cfg, "capture", "interval", 0.5)),
        )
        self.reader.on_final = lambda line: CONSOLE.line(
            f"{colors.dim('[' + _ts() + ']', colors.COLOR_STDOUT)} "
            f"{colors.caption(line, colors.COLOR_STDOUT)}"
        )
        self.reader.on_live = lambda text: CONSOLE.live(text)

        self.writer = ScoutWriter(
            exe=str(_cfg(cfg, "clawpilot", "exe",
                         r"C:\Program Files (x86)\Clawpilot\Clawpilot.exe")),
            window_title=str(_cfg(cfg, "clawpilot", "window_title", "Clawpilot")),
            restore_clipboard=bool(_cfg(cfg, "send", "restore_clipboard", True)),
        )
        self.foreground_on_start = bool(
            _cfg(cfg, "clawpilot", "foreground_on_start", True)
        )
        self.watchdog_interval = float(_cfg(cfg, "watchdog", "interval", 5.0))

        self.last_idx = 0
        # The in-progress (not-yet-finalized) caption line captured at the exact
        # instant the hotkey was pressed. Held until the send actually fires so
        # a busy-wait (Clawpilot mid-answer) or a transient empty read can't drop
        # the partial the user was looking at when they pressed.
        self._pending_live: str | None = None
        self._fire = threading.Event()
        self._stop = threading.Event()

    # -- hotkey ------------------------------------------------------------- #
    def _on_hotkey(self) -> None:
        # Runs in the HotkeyMonitor thread: do NOT touch UIA here. Reading the
        # reader's cached live line is just a locked attribute read (no UIA), so
        # we snapshot the partial *now* — at press time — not later at send time.
        _log("\u2328 hotkey detected")
        self._pending_live = self.reader.latest_live()
        self._fire.set()

    # -- the send routine (main thread only) -------------------------------- #
    def _do_send(self) -> str:
        """Attempt one send. Returns a status: 'busy', 'sent', 'nothing', 'error'."""
        try:
            if self.writer.is_generating():
                return "busy"

            if self.mode == "full":
                lines = self.reader.get_full()
                new_idx = len(lines)
            else:
                lines, new_idx = self.reader.get_delta(self.last_idx)

            # Include the in-progress (not-yet-finalized) caption line. Prefer
            # whatever is live right now; fall back to the partial captured at
            # the moment the hotkey was pressed (self._pending_live), so a
            # busy-wait delay or a transient empty read can't drop it. Skip it
            # only when it would merely duplicate the last finalized line.
            live_sent = False
            if self.include_live:
                live = self.reader.latest_live() or self._pending_live
                if live and (not lines or lines[-1] != live):
                    lines = lines + [live]
                    live_sent = True

            if not lines:
                _log("[nothing new to send]")
                return "nothing"

            payload = "\n".join(lines)
            payload = self._cap(payload)
            rendered = self.template.replace("{payload}", payload)

            self.writer.send(rendered)
            self.last_idx = new_idx
            self._pending_live = None
            _log(f"[sent {len(lines)} line(s) / {len(payload)} chars"
                 f"{' +live partial' if live_sent else ''}]")
            return "sent"
        except ScoutError as exc:
            _err(f"send failed: {exc}")
            return "error"
        except Exception as exc:  # keep the daemon alive no matter what
            _err(f"unexpected send error: {exc!r}")
            return "error"

    def _cap(self, payload: str) -> str:
        if len(payload) <= self.max_chars:
            return payload
        # Keep the most recent text; trim from the front on a line boundary.
        keep = self.max_chars - len(TRIM_MARKER)
        if keep <= 0:
            return payload[-self.max_chars:]
        tail = payload[-keep:]
        nl = tail.find("\n")
        if nl != -1:
            tail = tail[nl + 1:]
        return TRIM_MARKER + tail

    # -- watchdog ----------------------------------------------------------- #
    def _watchdog(self) -> None:
        try:
            if not self.writer.is_running():
                _log("[clawpilot not running — relaunching]")
                self.writer.ensure_running()
                self.writer.bring_to_front()
                _log("[clawpilot relaunched]")
        except Exception as exc:
            _err(f"watchdog error: {exc!r}")

    # -- run ---------------------------------------------------------------- #
    def run(self) -> int:
        combo_raw = str(_cfg(self.cfg, "hotkey", "combo", "win+ctrl+alt"))
        win_s = float(_cfg(self.cfg, "hotkey", "window", 0.25))
        try:
            monitor = HotkeyMonitor(combo_raw, self._on_hotkey, window=win_s)
        except ValueError as exc:
            _err(f"bad hotkey combo {combo_raw!r}: {exc} — falling back to win+ctrl+alt")
            monitor = HotkeyMonitor("win+ctrl+alt", self._on_hotkey, window=win_s)
        _log(f"souffleur daemon starting (hotkey: {pretty_combo(combo_raw)})")

        # 1) Clawpilot up + (optionally) foreground.
        try:
            self.writer.ensure_running()
            if self.foreground_on_start:
                self.writer.bring_to_front()
            self.writer.prewarm()  # cache the composer subtree up front
            _log("[clawpilot ready]")
        except Exception as exc:
            _err(f"could not start Clawpilot: {exc!r} (will retry via watchdog)")

        # 2) transcript reader.
        self.reader.start()
        _log("[transcript reader started — waiting for Teams captions]")

        # 3) global hotkey monitor (GetAsyncKeyState polling).
        monitor.start()
        _log("ready. Press the hotkey to send the transcript. Ctrl+C to quit.")

        # 4) main loop: serve hotkey fires + run watchdog between them.
        #    A press sets a pending request; if Clawpilot is mid-answer we hold
        #    it and retry every retry_interval until it goes idle (or we give up
        #    after idle_timeout), so a press is never silently dropped.
        last_wd = 0.0
        pending = False
        pending_since = 0.0
        last_attempt = 0.0
        try:
            while not self._stop.is_set():
                fired = self._fire.wait(timeout=0.2)
                now = time.monotonic()
                if fired:
                    self._fire.clear()
                    pending = True
                    pending_since = now
                    last_attempt = 0.0  # attempt immediately

                if pending and (now - last_attempt) >= self.retry_interval:
                    last_attempt = now
                    status = self._do_send()
                    if status == "busy":
                        if not self.wait_for_idle:
                            _log("[skipped: Clawpilot is generating]")
                            pending = False
                        elif now - pending_since >= self.idle_timeout:
                            _log(f"[gave up: Clawpilot still generating after "
                                 f"{self.idle_timeout:.0f}s]")
                            pending = False
                        elif now - pending_since < self.retry_interval * 1.5:
                            _log("[Clawpilot busy — will send when it's free]")
                        # else: keep retrying quietly
                    else:
                        pending = False

                now = time.monotonic()
                if now - last_wd >= self.watchdog_interval:
                    last_wd = now
                    self._watchdog()
        except KeyboardInterrupt:
            _log("shutting down...")
        finally:
            try:
                monitor.stop()
            except Exception:
                pass
            self.reader.stop()
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="souffleur daemon: live Teams transcript -> Clawpilot on a hotkey."
    )
    p.add_argument(
        "-c", "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"path to config.toml (default: {DEFAULT_CONFIG_PATH})",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    return Prompter(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
