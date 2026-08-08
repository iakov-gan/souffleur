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
  * Clawpilot is NOT monitored in the background. It is checked only on a hotkey
    press: if it isn't running it is launched, otherwise it is just brought to
    the front — then the transcript is sent (even if Clawpilot is mid-answer).

Usage:
    souffleur run                       # preferred entry point
    python -m souffleur.daemon          # uses ./config.toml (auto-created)
    python -m souffleur.daemon --config x.toml
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

from .teams_ui import TranscriptReader
from .scout import (
    DEFAULT_EXE,
    ScoutError,
    ScoutWriter,
    resolve_clawpilot_exe,
)
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
enabled = false
exe = "auto"
window_title = "Clawpilot"
foreground_on_start = true

[send]
mode = "delta"
max_chars = 12000
include_live = true
restore_clipboard = true
template = "Here is a transcript of the meeting (or follow-up):\\n'''\\n{payload}\\n'''\\nFind the latest question(s) and answer as an expert."

[capture]
interval = 0.5
auto_enable = true
save = true
directory = "~/.souffleur"
"""

TRIM_MARKER = "...[earlier transcript trimmed]...\n"
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


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


def safe_filename_component(value: str, fallback: str = "MEETING") -> str:
    """Make user-controlled text safe as one Windows filename component."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = re.sub(r"-{2,}", "-", value)
    if not value:
        value = fallback
    if value.upper() in _WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value[:120].rstrip(" .") or fallback


class MeetingRecorder:
    """Persist a live Markdown snapshot of the current meeting transcript."""

    RESUME_WINDOW_SECONDS = 12 * 60 * 60
    MEETING_KEY_PREFIX = "<!-- souffleur-meeting-key: "
    LIVE_MARKER = "<!-- souffleur-live-caption -->"

    def __init__(self, directory: str | Path, started_at: datetime | None = None):
        self.directory = Path(os.path.expandvars(str(directory))).expanduser()
        self.started_at = started_at or datetime.now()
        self.path: Path | None = None
        self.meeting_name = ""
        self.meeting_id: tuple | None = None
        self.finalized: list[str] = []
        self.live: str | None = None
        self._meeting_key = ""
        self._resume_tail: list[str] = []
        self._lock = threading.Lock()

    @staticmethod
    def _key(name: str, meeting_id: tuple | None) -> str:
        return json.dumps(
            [name, list(meeting_id) if meeting_id else None],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def _read_existing(
        self, path: Path
    ) -> tuple[str | None, datetime | None, list[str]]:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None, None, []

        key = None
        started_at = None
        for line in content.splitlines()[:8]:
            if line.startswith(self.MEETING_KEY_PREFIX) and line.endswith(" -->"):
                key = line[len(self.MEETING_KEY_PREFIX):-4]
            elif line.startswith("- Started: "):
                try:
                    started_at = datetime.fromisoformat(line[len("- Started: "):])
                except ValueError:
                    pass

        marker = "## Transcript\n\n"
        _, found, body = content.partition(marker)
        if not found:
            return key, started_at, []
        body = body.partition(f"\n\n{self.LIVE_MARKER}\n")[0].strip()
        return key, started_at, body.split("\n\n") if body else []

    def _find_resume_path(self, meeting_key: str) -> Path | None:
        now = datetime.now().timestamp()
        exact_matches = []
        legacy_matches = []
        for path in self.directory.glob("*.md"):
            key, _, _ = self._read_existing(path)
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if (
                key == meeting_key
                and now - modified <= self.RESUME_WINDOW_SECONDS
            ):
                exact_matches.append((modified, path))
            elif key is None and now - modified <= self.RESUME_WINDOW_SECONDS:
                try:
                    with path.open(encoding="utf-8") as stream:
                        first_line = stream.readline().rstrip()
                except OSError:
                    continue
                if first_line == f"# {self.meeting_name}":
                    legacy_matches.append((modified, path))

        matches = exact_matches or legacy_matches
        return max(matches, default=(0, None))[1]

    def start_meeting(
        self,
        name: str,
        meeting_id: tuple | None = None,
        started_at: datetime | None = None,
    ) -> Path:
        with self._lock:
            identity = (name, meeting_id)
            if self.path is not None and identity == self.meeting_id:
                return self.path

            self.started_at = started_at or datetime.now()
            self.meeting_name = name or "Microsoft Teams Meeting"
            self.meeting_id = identity
            self._meeting_key = self._key(self.meeting_name, meeting_id)
            self.finalized.clear()
            self.live = None
            self._resume_tail.clear()
            self.directory.mkdir(parents=True, exist_ok=True)

            resumed = self._find_resume_path(self._meeting_key)
            if resumed is not None:
                _, original_start, self.finalized = self._read_existing(resumed)
                if original_start is not None:
                    self.started_at = original_start
                self._resume_tail = self.finalized[-20:]
                self.path = resumed
                return resumed

            safe_name = safe_filename_component(self.meeting_name)
            stamp = self.started_at.strftime("%Y-%m-%d-%H-%M")
            candidate = self.directory / f"{stamp}-{safe_name}.md"
            suffix = 2
            while candidate.exists():
                candidate = self.directory / f"{stamp}-{safe_name}-{suffix}.md"
                suffix += 1
            self.path = candidate
            self._write(durable=True)
            return candidate

    def add_final(self, line: str) -> None:
        with self._lock:
            if self.path is None:
                return
            if self._resume_tail:
                try:
                    replay_index = self._resume_tail.index(line)
                except ValueError:
                    self._resume_tail.clear()
                else:
                    del self._resume_tail[:replay_index + 1]
                    return
            self.finalized.append(line)
            if self.live == line:
                self.live = None
            self._write(durable=True)

    def set_live(self, line: str | None) -> None:
        with self._lock:
            if self.path is None:
                return
            self.live = line
            self._write(durable=False)

    def _write(self, *, durable: bool) -> None:
        if self.path is None:
            return
        path = self.path
        lines = list(self.finalized)
        body = "\n\n".join(lines)
        content = (
            f"# {self.meeting_name}\n\n"
            f"- Started: {self.started_at.astimezone().isoformat(timespec='seconds')}\n"
            f"- Source: Microsoft Teams live captions\n\n"
            f"{self.MEETING_KEY_PREFIX}{self._meeting_key} -->\n\n"
            f"## Transcript\n\n{body}"
        )
        if body:
            content += "\n"
        if self.live and (not lines or lines[-1] != self.live):
            content += f"\n{self.LIVE_MARKER}\n{self.live}\n"
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        temp_path.replace(path)


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
    def __init__(
        self,
        cfg: dict,
        clawpilot_enabled: bool | None = None,
    ):
        self.cfg = cfg
        self.mode = str(_cfg(cfg, "send", "mode", "delta")).lower()
        self.max_chars = int(_cfg(cfg, "send", "max_chars", 12000))
        self.include_live = bool(_cfg(cfg, "send", "include_live", True))
        self.template = str(_cfg(cfg, "send", "template",
                                 "Here is a transcript of the meeting (or follow-up):\n"
                                 "'''\n{payload}\n'''\n"
                                 "Find the latest question(s) and answer as an expert."))

        self.reader = TranscriptReader(
            interval=float(_cfg(cfg, "capture", "interval", 0.5)),
            auto_enable=bool(_cfg(cfg, "capture", "auto_enable", True)),
        )
        self.recorder = (
            MeetingRecorder(_cfg(cfg, "capture", "directory", "~/.souffleur"))
            if bool(_cfg(cfg, "capture", "save", True))
            else None
        )

        def on_final(line: str) -> None:
            CONSOLE.line(
                f"{colors.dim('[' + _ts() + ']', colors.COLOR_STDOUT)} "
                f"{colors.caption(line, colors.COLOR_STDOUT)}"
            )
            if self.recorder:
                self.recorder.add_final(line)

        def on_live(text: str | None) -> None:
            CONSOLE.live(text)
            if self.recorder:
                self.recorder.set_live(text)

        def on_meeting_change(name: str, meeting_id: tuple | None) -> None:
            self.last_idx = 0
            if self.recorder:
                path = self.recorder.start_meeting(name, meeting_id)
                _log(f"[meeting changed — saving transcript to {path}]")

        self.reader.on_final = on_final
        self.reader.on_live = on_live
        self.reader.on_meeting_change = on_meeting_change
        self.reader.on_status = lambda message: _log(f"[captions: {message}]")

        self.writer = ScoutWriter(
            exe=str(_cfg(cfg, "clawpilot", "exe", DEFAULT_EXE)),
            window_title=str(_cfg(cfg, "clawpilot", "window_title", "Clawpilot")),
            restore_clipboard=bool(_cfg(cfg, "send", "restore_clipboard", True)),
        )
        configured_clawpilot = bool(
            _cfg(cfg, "clawpilot", "enabled", False)
        )
        self.clawpilot_enabled = (
            configured_clawpilot
            if clawpilot_enabled is None
            else clawpilot_enabled
        )
        self.foreground_on_start = bool(
            _cfg(cfg, "clawpilot", "foreground_on_start", True)
        )

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
        """Attempt one send. Returns a status: 'sent', 'nothing', 'error'.

        The transcript is pushed even if Clawpilot is mid-answer — the send
        routine pastes into the composer and submits without touching Stop.
        """
        try:
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

        # 1) Clawpilot up + (optionally) foreground. This is a one-time prime;
        #    Clawpilot is NOT monitored afterwards — each hotkey send re-checks
        #    it (launch if gone, else just bring to front).
        if self.clawpilot_enabled:
            try:
                self.writer.exe = resolve_clawpilot_exe(self.writer.exe)
            except ScoutError:
                self.clawpilot_enabled = False
                _log("[Clawpilot not installed — continuing in capture-only mode]")

        if self.clawpilot_enabled:
            try:
                self.writer.ensure_running()
                if self.foreground_on_start:
                    self.writer.bring_to_front()
                self.writer.prewarm()  # cache the composer subtree up front
                _log("[clawpilot ready]")
            except Exception as exc:
                _err(f"could not start Clawpilot: {exc!r} (will launch on hotkey)")

        # 2) Transcript reader. Its meeting-change callback creates/rotates the
        # output file before any rows from that meeting are emitted.
        self.reader.start()
        _log("[transcript reader started — waiting for Teams captions]")

        # 3) global hotkey monitor (GetAsyncKeyState polling).
        if self.clawpilot_enabled:
            monitor.start()
            _log("ready. Press the hotkey to send the transcript. Ctrl+C to quit.")
        else:
            _log("ready. Capturing and saving Teams captions. Ctrl+C to quit.")

        # 4) main loop: serve hotkey fires. Each press sends once, launching or
        #    fronting Clawpilot as needed and pushing the transcript even if
        #    Clawpilot is mid-answer. No background monitoring between presses.
        try:
            while not self._stop.is_set():
                if self._fire.wait(timeout=0.2):
                    self._fire.clear()
                    self._do_send()
        except KeyboardInterrupt:
            _log("shutting down...")
        finally:
            if self.clawpilot_enabled:
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
    p.add_argument(
        "--clawpilot",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable Clawpilot integration for this run",
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
    return Prompter(cfg, clawpilot_enabled=args.clawpilot).run()


if __name__ == "__main__":
    raise SystemExit(main())
