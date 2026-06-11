"""
scout.py — drive the Clawpilot / Microsoft Scout desktop app via UI Automation.

Sends text into the *current* Clawpilot chat the way a human would: it puts the
payload on the clipboard, focuses the message box, pastes, and clicks Send —
then restores the previous clipboard. It can also launch Clawpilot and bring it
to the foreground, and detect when Clawpilot is mid-generation (so we never
cancel an in-flight answer by clicking the Stop button).

Why clipboard-paste instead of typing? Clawpilot's input is an Electron
``contenteditable`` that (a) ignores UIA ValuePattern.SetValue and (b) treats
Enter as "send" — so typing a multiline transcript would submit early. Pasting
handles large, multiline text reliably.
"""
from __future__ import annotations

import subprocess
import sys
import time

import uiautomation as auto

DEFAULT_EXE = r"C:\Program Files (x86)\Clawpilot\Clawpilot.exe"
DEFAULT_TITLE = "Clawpilot"
SEND_BUTTON_NAME = "Send"
STOP_BUTTON_NAME = "Stop"
MESSAGE_EDIT_NAME = "Message"


class ScoutError(RuntimeError):
    pass


class ScoutWriter:
    """UI-Automation driver for the Clawpilot/Scout window."""

    def __init__(
        self,
        *,
        exe: str = DEFAULT_EXE,
        window_title: str = DEFAULT_TITLE,
        search_depth: int = 20,
        stop_depth: int = 16,
        restore_clipboard: bool = True,
    ):
        self.exe = exe
        self.window_title = window_title
        self.search_depth = search_depth
        self.stop_depth = stop_depth
        self.restore_clipboard = restore_clipboard
        self._msg: auto.Control | None = None  # cached Message edit (stable)

    # -- window discovery --------------------------------------------------- #
    def find_window(self) -> auto.Control | None:
        try:
            root = auto.GetRootControl()
            for w in root.GetChildren():
                try:
                    if (w.ControlTypeName == "WindowControl"
                            and w.Name == self.window_title):
                        return w
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _bfs_find(self, root: auto.Control, ctype: str | None, names,
                  maxdepth: int) -> auto.Control | None:
        """Find a descendant by Name (and optional ControlTypeName) using a
        breadth-first walk built on batched ``GetChildren`` calls.

        ``names`` may be a single name or a tuple of acceptable names.

        This is dramatically faster than ``uiautomation``'s native
        ``Control(Name=...).Exists()`` on Electron apps: that walker visits the
        tree node-by-node (a COM round-trip per node *plus* a Name fetch),
        which is pathological when Clawpilot's chat history holds thousands of
        nodes. ``GetChildren`` fetches whole sibling sets in one call. To avoid
        scanning the (huge) chat history at all, callers pass a small ``root``
        (the cached composer subtree) rather than the whole window.
        """
        if isinstance(names, str):
            names = (names,)
        level = [(root, 0)]
        while level:
            nxt = []
            for ctrl, depth in level:
                try:
                    if ctrl.Name in names and (
                            ctype is None or ctrl.ControlTypeName == ctype):
                        return ctrl
                except Exception:
                    pass
                if depth < maxdepth:
                    try:
                        for kid in ctrl.GetChildren():
                            nxt.append((kid, depth + 1))
                    except Exception:
                        pass
            level = nxt
        return None

    def _input(self, win: auto.Control):
        """Return ``(msg, scope)``: the Message edit control and a *small*
        subtree that contains the Send/Stop action button.

        The Message box is stable across generation (only the Send↔Stop button
        re-renders), so we cache the Message element. The expensive full-window
        search (Clawpilot's chat history holds thousands of nodes) then runs at
        most once per genuine input rebuild; every other call just climbs a few
        parents from the cached Message box and searches the tiny composer
        subtree — sub-millisecond — instead of scanning the history.
        """
        msg = self._msg
        if msg is not None:
            try:
                _ = msg.Name  # validate cached element; raises if stale
            except Exception:
                msg = self._msg = None
        if msg is None:
            msg = self._bfs_find(win, "EditControl", MESSAGE_EDIT_NAME,
                                 self.search_depth)
            self._msg = msg
        if msg is None:
            return None, win

        button_names = (SEND_BUTTON_NAME, STOP_BUTTON_NAME)
        node = msg
        for up in range(8):
            try:
                parent = node.GetParentControl()
            except Exception:
                parent = None
            if parent is None:
                break
            node = parent
            if self._bfs_find(node, "ButtonControl", button_names, up + 2) is not None:
                return msg, node
        # Climb found no button — cached Message may be stale; re-find once.
        if self._msg is not None:
            self._msg = None
            return self._input(win)
        return msg, win

    def is_running(self) -> bool:
        return self.find_window() is not None

    def launch(self) -> bool:
        """Start Clawpilot if its executable exists. Returns True if spawned."""
        try:
            subprocess.Popen([self.exe], close_fds=True)
            return True
        except Exception as exc:
            raise ScoutError(f"could not launch Clawpilot ({self.exe!r}): {exc}")

    def ensure_running(self, wait: float = 25.0) -> auto.Control:
        """Return the window, launching Clawpilot and waiting if necessary."""
        win = self.find_window()
        if win is not None:
            return win
        self.launch()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(1.0)
            win = self.find_window()
            if win is not None:
                return win
        raise ScoutError(
            f"Clawpilot window {self.window_title!r} did not appear within "
            f"{wait:.0f}s of launch."
        )

    def bring_to_front(self, win: auto.Control | None = None) -> None:
        win = win or self.find_window()
        if win is None:
            return
        for attempt in (lambda: win.SetActive(),
                        lambda: (win.SetTopmost(True), win.SetTopmost(False))):
            try:
                attempt()
            except Exception:
                pass

    # -- state -------------------------------------------------------------- #
    def is_generating(self, win: auto.Control | None = None) -> bool:
        """True if Clawpilot is mid-answer (a Stop button is present).

        This is the one check that runs *while Clawpilot is generating*, when
        every UIA call is slow (the DOM mutates on each token). So we search
        only as deep as the Stop button lives (it sits a couple levels above the
        Message box) and stop the instant we find it — visiting ~90 nodes rather
        than the whole window, keeping the during-generation cost low.
        """
        win = win or self.find_window()
        if win is None:
            return False
        try:
            stop = self._bfs_find(win, "ButtonControl", STOP_BUTTON_NAME,
                                  self.stop_depth)
            return stop is not None
        except Exception:
            return False

    def prewarm(self) -> None:
        """Resolve and cache the Message box now (cheap while idle), so the
        first hotkey send doesn't pay the one-time full-window search."""
        win = self.find_window()
        if win is not None:
            try:
                self._input(win)
            except Exception:
                pass

    # -- sending ------------------------------------------------------------ #
    def send(self, text: str) -> None:
        """Paste ``text`` into the current chat and click Send.

        Raises ScoutError on any structural failure (window/edit/button
        missing). Skips silently only via the caller's is_generating() guard.
        """
        if not text or not text.strip():
            raise ScoutError("refusing to send empty text")

        win = self.ensure_running()
        self.bring_to_front(win)
        time.sleep(0.3)

        msg, scope = self._input(win)
        if msg is None:
            raise ScoutError("Clawpilot 'Message' input not found")
        depth = 8 if scope is not win else self.search_depth

        send_btn = self._bfs_find(scope, "ButtonControl", SEND_BUTTON_NAME, depth)
        if send_btn is None:
            raise ScoutError("Clawpilot 'Send' button not found")

        saved = None
        if self.restore_clipboard:
            try:
                saved = auto.GetClipboardText()
            except Exception:
                saved = None

        try:
            auto.SetClipboardText(text)
            msg.SetFocus()
            time.sleep(0.25)
            # Clear any draft, then paste.
            auto.SendKeys("{Ctrl}a{Delete}", waitTime=0.03)
            auto.SendKeys("{Ctrl}v", waitTime=0.05)
            time.sleep(0.35)
            self._click_send(win, send_btn)
        finally:
            if self.restore_clipboard and saved is not None:
                # Restore after a short delay so the paste already consumed it.
                time.sleep(0.4)
                try:
                    auto.SetClipboardText(saved)
                except Exception:
                    pass

    def _click_send(self, win: auto.Control, send_btn: auto.Control) -> None:
        # Prefer Invoke; fall back to a synthesized click. Re-resolve the button
        # in case the control was replaced between paste and click.
        btn = send_btn
        try:
            btn.GetInvokePattern().Invoke()
            return
        except Exception:
            pass
        _msg, scope = self._input(win)
        depth = 8 if scope is not win else self.search_depth
        btn = self._bfs_find(scope, "ButtonControl", SEND_BUTTON_NAME, depth)
        if btn is None:
            raise ScoutError("Send button vanished before click")
        try:
            btn.GetInvokePattern().Invoke()
        except Exception:
            try:
                btn.Click(simulateMove=False)
            except Exception as exc:
                raise ScoutError(f"failed to click Send: {exc}")


# --------------------------------------------------------------------------- #
# Manual smoke test:  python scout.py "some message"
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    text = " ".join(sys.argv[1:]) or "Hi it is test (scout.py smoke)"
    w = ScoutWriter()
    print("running:", w.is_running())
    if w.is_generating():
        print("Clawpilot is generating — skipping send.")
        sys.exit(0)
    w.send(text)
    print(f"sent: {text!r}")
