"""
teams_ui.py — reusable Microsoft Teams live-caption reader (UI Automation).

This module holds the language-independent, self-healing caption-capture core
that used to live inside ``soufleur.py``. It exposes both:

  * the low-level primitives (find_container, read_rows, CaptionTracker, ...)
    that the ``soufleur`` CLI re-uses for its capture/discover/doctor commands;
  * a high-level ``TranscriptReader`` that runs a background thread, keeps an
    in-memory buffer of finalized caption lines, and lets a consumer pull the
    full transcript or just the delta since a given index.

Nothing here writes to disk or leaves the machine — it only reads the text the
local Teams client is already drawing on screen.
"""
from __future__ import annotations

import threading
import time

import uiautomation as auto

CAPTION_HINTS = ("caption", "captions", "live caption", "transcript")
# Each caption entry in new Teams is rendered as this Fluent UI element,
# whose children are [author TextControl, caption-text TextControl].
BODY_CLASS = "fui-ChatMessageCompact__body"


# --------------------------------------------------------------------------- #
# Window / container discovery
# --------------------------------------------------------------------------- #
def is_teams_window(win: auto.Control) -> bool:
    try:
        name = (win.Name or "").lower()
        cls = (win.ClassName or "").lower()
    except Exception:
        return False
    if "teams" in name:
        return True
    if cls in ("chrome_widgetwin_1", "chrome_widgetwin_0") and name:
        return True
    return False


def iter_teams_windows():
    root = auto.GetRootControl()
    # Prefer an active meeting window if one exists.
    wins = []
    for win in root.GetChildren():
        try:
            if is_teams_window(win):
                wins.append(win)
        except Exception:
            continue
    wins.sort(key=lambda w: 0 if "meeting" in (w.Name or "").lower() else 1)
    return wins


def _attrs(ctrl: auto.Control):
    try:
        return (
            (ctrl.Name or ""),
            (ctrl.AutomationId or ""),
            (ctrl.ClassName or ""),
        )
    except Exception:
        return "", "", ""


def _cls0(cls: str) -> str:
    return (cls or "").split(" ")[0]


def looks_like_caption(ctrl: auto.Control) -> bool:
    name, aid, cls = _attrs(ctrl)
    blob = " ".join((name, aid, cls)).lower()
    return any(h in blob for h in CAPTION_HINTS)


def walk(ctrl: auto.Control, depth: int, max_depth: int):
    if depth > max_depth:
        return
    yield depth, ctrl
    try:
        children = ctrl.GetChildren()
    except Exception:
        children = []
    for child in children:
        yield from walk(child, depth + 1, max_depth)


def count_bodies(ctrl: auto.Control, max_depth: int = 30) -> int:
    total = 0
    for _, c in walk(ctrl, 0, max_depth):
        try:
            if _cls0(c.ClassName or "") == BODY_CLASS:
                total += 1
        except Exception:
            pass
    return total


def _runtime_id(ctrl: auto.Control):
    try:
        rid = ctrl.GetRuntimeId()
        return tuple(rid) if rid else None
    except Exception:
        return None


def _ancestor_chain(ctrl: auto.Control, win: auto.Control) -> list[auto.Control]:
    """Return the chain of controls [win, ..., ctrl], or [] if not under win."""
    win_rid = _runtime_id(win)
    chain: list[auto.Control] = []
    cur = ctrl
    for _ in range(200):  # guard against cycles / runaway walks
        if cur is None:
            return []
        chain.append(cur)
        if win_rid is not None and _runtime_id(cur) == win_rid:
            chain.reverse()
            return chain
        try:
            cur = cur.GetParentControl()
        except Exception:
            return []
    return []


def _lca_container(win: auto.Control, max_depth: int):
    """Lowest common ancestor of all caption bodies under ``win``.

    This is language-independent: it does not rely on the region's Name (which
    Teams localizes when the UI language changes). Returns (control, count).
    """
    bodies: list[auto.Control] = []
    collect_bodies(win, bodies, max_depth)
    if not bodies:
        return None, 0
    chains = [c for c in (_ancestor_chain(b, win) for b in bodies) if c]
    if not chains:
        return None, 0
    rid_chains = [[_runtime_id(c) for c in ch] for ch in chains]
    minlen = min(len(c) for c in rid_chains)
    common = 0
    for i in range(minlen):
        col = {c[i] for c in rid_chains}
        if len(col) == 1 and next(iter(col)) is not None:
            common = i + 1
        else:
            break
    if common == 0:
        return win, len(bodies)
    return chains[0][common - 1], len(bodies)


def find_container(
    container_aid: str | None,
    container_name: str | None,
    max_depth: int,
) -> auto.Control | None:
    """Locate the 'Live Captions' region across all Teams windows.

    Strategy:
      1. honor an explicit --container-aid / --container-name override;
      2. prefer a region whose Name hints at captions (fast path, English UI);
      3. otherwise fall back to the lowest common ancestor of the caption
         bodies, which works regardless of the Teams display language.
    """
    # 1) explicit override requested by the user
    if container_aid or container_name:
        for win in iter_teams_windows():
            for _, ctrl in walk(win, 0, max_depth):
                name, aid, _ = _attrs(ctrl)
                if container_aid and aid == container_aid:
                    return ctrl
                if container_name and container_name.lower() in name.lower() and name:
                    return ctrl
        return None

    # 2) auto-detect via a caption-named region (only works when the Teams UI
    #    language is one whose region label contains a known hint word).
    best = None
    best_score = -1
    for win in iter_teams_windows():
        for _, ctrl in walk(win, 0, max_depth):
            name, _, _ = _attrs(ctrl)
            blob = name.lower()
            if name and any(h in blob for h in CAPTION_HINTS):
                score = 1000 + count_bodies(ctrl)
                if score > best_score:
                    best_score, best = score, ctrl
    if best is not None:
        return best

    # 3) language-independent fallback: the lowest common ancestor of the
    #    caption-message bodies. Robust when the region Name is localized.
    best, best_n = None, 0
    for win in iter_teams_windows():
        cand, n = _lca_container(win, max_depth)
        if cand is not None and n > best_n:
            best, best_n = cand, n
    return best


# --------------------------------------------------------------------------- #
# Reading caption rows
# --------------------------------------------------------------------------- #
def leaf_texts(ctrl: auto.Control, max_depth: int = 6) -> list[str]:
    out = []
    try:
        children = ctrl.GetChildren()
    except Exception:
        children = []
    if not children or max_depth <= 0:
        try:
            t = (ctrl.Name or "").strip()
        except Exception:
            t = ""
        return [t] if t else []
    for child in children:
        out.extend(leaf_texts(child, max_depth - 1))
    return out


def collect_bodies(ctrl: auto.Control, out: list, max_depth: int = 30):
    if max_depth < 0:
        return
    try:
        if _cls0(ctrl.ClassName or "") == BODY_CLASS:
            out.append(ctrl)
    except Exception:
        pass
    try:
        children = ctrl.GetChildren()
    except Exception:
        children = []
    for child in children:
        collect_bodies(child, out, max_depth - 1)


def read_rows(container: auto.Control) -> list[dict]:
    """Return caption rows in display order: [{'speaker','text','raw'}].

    Each caption entry is a `fui-ChatMessageCompact__body` whose children are
    [author, caption-text]. We read its leaf texts and split author from text.
    """
    bodies: list[auto.Control] = []
    collect_bodies(container, bodies)
    rows = []
    for body in bodies:
        leaves = [t for t in leaf_texts(body) if t]
        if not leaves:
            continue
        if len(leaves) >= 2:
            speaker, text = leaves[0], " ".join(leaves[1:])
        else:
            speaker, text = "", leaves[0]
        rows.append({"speaker": speaker, "text": text, "raw": leaves})
    return rows


def row_key(row: dict) -> str:
    return f"{row['speaker']}\u241f{row['text']}"


def format_row(row: dict) -> str:
    """Render a caption row as a single line.

    Speakers are wrapped in angle brackets ('<Speaker:> text') so a downstream
    LLM can unambiguously tell turns apart even when the text itself contains
    colons or newlines were stripped. Lines without a known speaker are emitted
    verbatim.
    """
    return f"<{row['speaker']}:> {row['text']}" if row.get("speaker") else row["text"]


def describe(ctrl: auto.Control) -> str:
    name, aid, cls = _attrs(ctrl)
    try:
        ct = ctrl.ControlTypeName
    except Exception:
        ct = "?"
    snippet = name if len(name) <= 70 else name[:67] + "..."
    return f"[{ct}] AID={aid!r} Cls0={_cls0(cls)!r} Name={snippet!r}"


# --------------------------------------------------------------------------- #
# Finalization tracker (handles the scrolling, in-place-growing last line)
# --------------------------------------------------------------------------- #
class CaptionTracker:
    def __init__(self):
        self.history: list[str] = []  # finalized row keys, in order

    def update(self, rows: list[dict]) -> list[dict]:
        """Feed current visible rows; return rows newly considered final."""
        if not rows:
            return []
        # last row is "live" (still changing); everything before is final
        finals = rows[:-1]
        if not finals:
            return []
        keys = [row_key(r) for r in finals]
        # align: longest suffix of history that prefixes the new finals
        maxk = min(len(self.history), len(keys))
        overlap = 0
        for k in range(maxk, 0, -1):
            if self.history[-k:] == keys[:k]:
                overlap = k
                break
        new = finals[overlap:]
        self.history.extend(keys[overlap:])
        return new


# --------------------------------------------------------------------------- #
# High-level background reader
# --------------------------------------------------------------------------- #
class TranscriptReader:
    """Background thread that keeps an in-memory transcript of Teams captions.

    Maintains a growing list of *finalized* caption lines (speaker-prefixed)
    plus the current *live* (still-changing) line. Self-heals across language
    changes / panel toggles / meeting restarts by re-acquiring the caption
    container after a short run of empty reads — mirroring the CLI capture loop.

    Thread-safe accessors:
        get_full()            -> list[str]            (all finalized lines)
        get_delta(since)      -> (list[str], int)     (lines after `since`, new len)
        latest_live()         -> str | None           (current in-progress line)
        is_healthy()          -> bool                  (container acquired + reading)
        line_count()          -> int                   (number of finalized lines)
    """

    # Backoff bounds (seconds) used while waiting for live captions to appear.
    SEARCH_MIN = 0.5
    SEARCH_MAX = 3.0

    def __init__(
        self,
        *,
        depth: int = 40,
        interval: float = 0.25,
        container_aid: str | None = None,
        container_name: str | None = None,
    ):
        self.depth = depth
        self.interval = max(0.05, interval)
        self.container_aid = container_aid
        self.container_name = container_name

        self._finalized: list[str] = []
        self._live: str | None = None
        self._healthy = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Optional callback(line: str) invoked for each newly finalized line.
        self.on_final = None
        # Optional callback(text: str | None) invoked when the in-progress
        # (live) caption line changes — for real-time display of a paragraph
        # that is still growing before it finalizes.
        self.on_live = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> "TranscriptReader":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="TranscriptReader", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- accessors ---------------------------------------------------------- #
    def get_full(self) -> list[str]:
        with self._lock:
            return list(self._finalized)

    def get_delta(self, since_index: int) -> tuple[list[str], int]:
        """Return finalized lines after ``since_index`` and the new index."""
        with self._lock:
            if since_index < 0:
                since_index = 0
            return list(self._finalized[since_index:]), len(self._finalized)

    def latest_live(self) -> str | None:
        with self._lock:
            return self._live

    def line_count(self) -> int:
        with self._lock:
            return len(self._finalized)

    def is_healthy(self) -> bool:
        return self._healthy

    # -- worker ------------------------------------------------------------- #
    def _acquire(self) -> auto.Control | None:
        return find_container(self.container_aid, self.container_name, self.depth)

    def _append_finals(self, new_finals: list[dict]) -> None:
        if not new_finals:
            return
        lines = [format_row(r) for r in new_finals]
        with self._lock:
            self._finalized.extend(lines)
        if self.on_final:
            for line in lines:
                try:
                    self.on_final(line)
                except Exception:
                    pass

    def _run(self) -> None:
        # Each UIA call should fail fast so the loop stays responsive.
        try:
            auto.SetGlobalSearchTimeout(1)
        except Exception:
            pass

        tracker = CaptionTracker()
        container: auto.Control | None = None
        empty_polls = 0
        reacquire_after = max(1, int(2.0 / self.interval))
        backoff = self.SEARCH_MIN

        while not self._stop.is_set():
            # --- SEARCHING: no usable container yet ------------------------ #
            if container is None:
                container = self._acquire()
                if container is None:
                    self._healthy = False
                    self._stop.wait(backoff)
                    backoff = min(backoff * 1.6, self.SEARCH_MAX)
                    continue
                backoff = self.SEARCH_MIN
                empty_polls = 0
                self._healthy = True

            # --- CAPTURING ------------------------------------------------- #
            try:
                rows = read_rows(container)
            except Exception:
                rows = []

            if not rows:
                empty_polls += 1
                if empty_polls >= reacquire_after:
                    empty_polls = 0
                    fresh = self._acquire()
                    if fresh is None:
                        self._healthy = False
                        container = None
                        continue
                    container = fresh
                    try:
                        rows = read_rows(container)
                    except Exception:
                        rows = []
            else:
                empty_polls = 0
                self._healthy = True

            self._append_finals(tracker.update(rows))

            new_live = format_row(rows[-1]) if rows else None
            with self._lock:
                changed = new_live != self._live
                self._live = new_live
            if changed and self.on_live:
                try:
                    self.on_live(new_live)
                except Exception:
                    pass

            self._stop.wait(self.interval)

        self._healthy = False
