"""
souffleur.cli  —  local, real-time Teams live-caption capture.

Reads Microsoft Teams *Live Captions* straight from the Windows UI Automation
(accessibility) tree of the Teams window on this PC. No bot, no Graph API, no
recording uploaded anywhere, nothing visible to the tenant — it only reads the
text your own Teams client is already drawing on your screen.

PREREQUISITE: in the meeting, turn ON live captions:
    More (...) > Language and speech > Turn on live captions

Quick start (use ``souffleur`` once installed, or ``python -m souffleur``):
    souffleur                 # daemon: transcript -> Clawpilot on a hotkey (default)
    souffleur capture         # just tail live captions to stdout
    souffleur discover        # diagnose: show windows + caption region
    souffleur discover --tree # also dump the meeting window UIA subtree
    souffleur doctor          # one-shot "is everything OK?" check
    souffleur run             # explicit form of the default daemon mode

The capture loop self-heals: it waits (forever by default) until live captions
appear, and automatically re-acquires the caption region after a language
change, a captions-panel toggle, or a meeting restart. Lines are printed to
stdout only; nothing is written to disk and nothing leaves your PC.

This is for capturing meetings you are entitled to capture. Confirm consent
and your organization's policy before using it.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# Teams captions contain non-Latin text; force UTF-8 console output.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import uiautomation as auto

# The caption-reading core now lives in teams_ui.py and is shared with the
# daemon. Re-import the primitives the CLI commands below rely on.
from .teams_ui import (
    CAPTION_HINTS,
    BODY_CLASS,
    CaptionTracker,
    _attrs,
    _cls0,
    count_bodies,
    describe,
    find_container,
    iter_teams_windows,
    read_rows,
    walk,
)

# Backoff bounds (seconds) used while waiting for live captions to appear.
SEARCH_MIN = 0.5
SEARCH_MAX = 3.0


def _eprint(*args, **kwargs) -> None:
    """Status/heartbeat output goes to stderr so stdout stays caption-only."""
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def acquire(args) -> auto.Control | None:
    """Find the caption container, honoring any explicit overrides."""
    aid = getattr(args, "container_aid", None)
    name = getattr(args, "container_name", None)
    return find_container(aid, name, args.depth)


# --------------------------------------------------------------------------- #
# capture: self-healing tail loop (SEARCHING <-> CAPTURING)
# --------------------------------------------------------------------------- #
def run_capture(args) -> int:
    auto.SetGlobalSearchTimeout(1)

    tracker = CaptionTracker()
    container: auto.Control | None = None
    last_live = ""
    live_active = False  # is an in-place (live) line currently drawn on screen?
    empty_polls = 0
    reacquire_after = max(1, int(2.0 / max(args.interval, 0.01)))
    backoff = SEARCH_MIN
    searching_announced = False
    timeout = args.timeout if args.timeout and args.timeout > 0 else 0.0
    search_deadline = (time.monotonic() + timeout) if timeout else None

    _eprint("souffleur: waiting for Teams live captions... (Ctrl+C to stop)")
    try:
        while True:
            # --- SEARCHING: no usable container yet -------------------------
            if container is None:
                container = acquire(args)
                if container is None:
                    if search_deadline and time.monotonic() > search_deadline:
                        _eprint("souffleur: timed out waiting for live captions.")
                        return 1
                    if not searching_announced:
                        _eprint("souffleur: no live captions yet — turn them on "
                                "(More > Language and speech). waiting...")
                        searching_announced = True
                    time.sleep(backoff)
                    backoff = min(backoff * 1.6, SEARCH_MAX)
                    continue
                # found a container -> reset search state, enter CAPTURING
                backoff = SEARCH_MIN
                searching_announced = False
                empty_polls = 0
                name, aid, cls = _attrs(container)
                _eprint(f"souffleur: capturing from "
                        f"[Class='{cls}' AID='{aid}' Name='{name[:40]}']")

            # --- CAPTURING --------------------------------------------------
            try:
                rows = read_rows(container)
            except Exception:
                rows = []

            if not rows:
                # A language switch / panel toggle rebuilds the subtree and our
                # reference goes stale (silent empty reads). Re-acquire; if the
                # region is truly gone, fall back to SEARCHING.
                empty_polls += 1
                if empty_polls >= reacquire_after:
                    empty_polls = 0
                    fresh = acquire(args)
                    if fresh is None:
                        _eprint("souffleur: caption region lost — re-searching...")
                        container = None
                        search_deadline = ((time.monotonic() + timeout)
                                           if timeout else None)
                        continue
                    container = fresh
                    try:
                        rows = read_rows(container)
                    except Exception:
                        rows = []
            else:
                empty_polls = 0

            new_finals = tracker.update(rows)
            # Erase the in-place (live) line before emitting permanent lines so
            # finals never get appended on top of a half-drawn live phrase.
            if new_finals and live_active:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()
                live_active = False
                last_live = ""  # force the live line to be redrawn afterwards
            for r in new_finals:
                line = f"{r['speaker']}: {r['text']}" if r["speaker"] else r["text"]
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] {line}", flush=True)

            if args.show_live and rows:
                live = rows[-1]
                live_line = (f"{live['speaker']}: {live['text']}"
                             if live["speaker"] else live["text"])
                if live_line and live_line != last_live:
                    last_live = live_line
                    # Keep the live line on a single terminal row; if it wraps,
                    # the leading \r\033[K can't fully clear it next time.
                    prefix = "    (live) "
                    width = shutil.get_terminal_size((120, 25)).columns
                    avail = max(20, width - len(prefix) - 1)
                    sys.stderr.write(f"\r\033[K{prefix}{live_line[:avail]}")
                    sys.stderr.flush()
                    live_active = True

            time.sleep(args.interval)
    except KeyboardInterrupt:
        if live_active:
            sys.stderr.write("\r\033[K")  # wipe the live line on the way out
            sys.stderr.flush()
        _eprint("\nsouffleur: stopped.")
    return 0


# --------------------------------------------------------------------------- #
# discover: diagnose windows + caption region (was discover.py)
# --------------------------------------------------------------------------- #
def cmd_discover(args) -> int:
    auto.SetGlobalSearchTimeout(2)

    windows = iter_teams_windows()
    if not windows:
        print("No Microsoft Teams windows found. Is Teams running?")
        return 1

    print("Teams windows:")
    for i, win in enumerate(windows):
        print(f"  [{i}] {describe(win)}")

    if args.tree:
        meeting = next(
            (w for w in windows if "meeting" in (w.Name or "").lower()), windows[0]
        )
        print(f"\n--- UIA subtree of: {(meeting.Name or '')[:60]!r} ---")
        for depth, ctrl in walk(meeting, 0, args.depth):
            print("  " * depth + describe(ctrl))

    print("\nLocating caption region...")
    container = find_container(None, None, args.depth)
    if container is None:
        print("  NOT FOUND.")
        print("  -> In the meeting: More (...) > Language and speech >")
        print("     Turn on live captions, then re-run.")
        return 1

    print(f"  Found: {describe(container)}")
    rows = read_rows(container)
    print(f"  Caption entries currently in the region: {len(rows)}")
    if rows:
        print("\n  Last few lines souffleur would capture:")
        for r in rows[-5:]:
            who = f"{r['speaker']}: " if r["speaker"] else ""
            print(f"    {who}{r['text'][:90]}")
    print("\nLooks good. Run:  python souffleur.py")
    return 0


# --------------------------------------------------------------------------- #
# doctor: one-shot readiness check
# --------------------------------------------------------------------------- #
def cmd_doctor(args) -> int:
    auto.SetGlobalSearchTimeout(2)
    ok = True

    wins = iter_teams_windows()
    print(f"Teams windows  : {len(wins)}"
          + ("" if wins else "   <- start Teams / join a meeting"))
    ok = ok and bool(wins)

    container = find_container(None, None, args.depth)
    if container is None:
        print("Caption region : NOT FOUND   <- turn ON live captions")
        ok = False
    else:
        name, _, _ = _attrs(container)
        rows = read_rows(container)
        print(f"Caption region : OK — {len(rows)} entries, Name={name[:40]!r}")

    print("\nVERDICT:",
          "ready to capture." if ok else "not ready — see above.")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _add_target_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--container-aid", help="AutomationId of caption container.")
    sp.add_argument("--container-name",
                    help="Name substring of caption container.")
    sp.add_argument("--depth", type=int, default=40, help="Max search depth.")


def cmd_run(args: argparse.Namespace) -> int:
    """Launch the souffleur daemon (transcript -> Clawpilot on a hotkey)."""
    _print_options_banner()
    # Imported lazily so `capture`/`discover`/`doctor` never pull in the Scout
    # writer or Clawpilot automation stack.
    from . import daemon
    cfg = daemon.load_config(args.config or daemon.DEFAULT_CONFIG_PATH)
    return daemon.Prompter(cfg).run()


def _print_options_banner() -> None:
    """Print a short reminder of the available modes when the daemon starts."""
    print(
        "\n"
        "Souffleur — daemon mode (default). Available commands (souffleur <cmd>):\n"
        "  run       (default)  daemon: Teams transcript -> Clawpilot on a hotkey\n"
        "  capture              just tail live captions to stdout\n"
        "  discover             diagnose windows + caption region (--tree for subtree)\n"
        "  doctor               one-shot readiness check\n"
        "  -h / --help          full option reference\n"
        "Daemon flags: -c/--config PATH  (config.toml: sets hotkey, template, etc.)\n"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="souffleur",
        description="Locally capture Microsoft Teams live captions via UI "
                    "Automation. Default action is 'run' (the daemon).")
    sub = p.add_subparsers(dest="cmd")

    cap = sub.add_parser("capture", help="Tail live captions to stdout.")
    _add_target_args(cap)
    cap.add_argument("--interval", type=float, default=0.25,
                     help="Polling interval in seconds (default 0.25).")
    cap.add_argument("--show-live", action="store_true",
                     help="Also show the in-progress line on stderr.")
    cap.add_argument("--timeout", type=float, default=0.0,
                     help="Seconds to wait for captions before giving up "
                          "(0 = wait forever, the default).")
    cap.set_defaults(func=run_capture)

    dis = sub.add_parser("discover", help="Diagnose windows + caption region.")
    dis.add_argument("--tree", action="store_true",
                     help="Dump the meeting window UIA subtree.")
    dis.add_argument("--depth", type=int, default=40, help="Max search depth.")
    dis.set_defaults(func=cmd_discover)

    doc = sub.add_parser("doctor", help="One-shot readiness check.")
    doc.add_argument("--depth", type=int, default=40, help="Max search depth.")
    doc.set_defaults(func=cmd_doctor)

    rn = sub.add_parser(
        "run",
        help="Run the souffleur daemon: transcript -> Clawpilot on a hotkey "
             "(default).")
    rn.add_argument("-c", "--config", type=Path, default=None,
                    help="path to config.toml (default: ./config.toml).")
    rn.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default to the `run` daemon subcommand when none is given. Bare flags that
    # aren't a known subcommand are routed to `run` too (e.g. `--config x`).
    known = {"capture", "discover", "doctor", "run", "-h", "--help"}
    if not argv or argv[0] not in known:
        argv = ["run"] + argv
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
