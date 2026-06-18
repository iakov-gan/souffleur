# Souffleur

Local, real-time **Microsoft Teams live-caption** capture for Windows that
**pushes the live transcript / captions to Microsoft Scout (Clawpilot) to get
fast answers in real time** — on a single hotkey.

Souffleur (French *souffleur* — a theatre prompter) reads the **Live Captions**
that the Teams desktop client already draws on *your* screen, straight from the
Windows **UI Automation** (accessibility) tree, and prints them to the console
in real time with speaker labels.

- ✅ 100% local — runs as a normal app on your PC
- ✅ No bot, no meeting join, no Microsoft Graph, no recording uploaded anywhere
- ✅ Invisible to the tenant — it only *reads text already rendered on your screen*
- ✅ Captures speaker names and any language Teams transcribes (e.g. Russian, etc.)
- ✅ **Self-healing** — survives caption-language changes, panel toggles and
  meeting restarts, and waits for captions to come on without exiting

## ⚠️ Before you use it

Capturing/transcribing a meeting can require the consent of participants and may
be governed by law and by your organization's policy. **You are responsible for
confirming you are allowed to capture a given meeting.** Use this only for
meetings you are entitled to capture.

## Requirements

- Windows
- Microsoft Teams (new/desktop) running a meeting with **Live Captions ON**
  - In the meeting: **More (...) → Language and speech → Turn on live captions**
- Python 3.10+

## Install

```powershell
git clone https://github.com/iakovgan_microsoft/souffleur.git
cd souffleur
pip install .          # installs the `souffleur` command + dependencies
```

Use `pip install -e .` for an editable/development install. This puts a
`souffleur` command on your PATH; you can also run the package without
installing via `python -m souffleur` (after `pip install -r requirements.txt`).

## Quick start

```powershell
# 1. In your Teams meeting: More (...) → Language and speech → Turn on live captions
# 2. (optional) check Souffleur can see the captions
souffleur doctor

# 3a. Run the daemon (default): transcript → Clawpilot on the Win+Ctrl+Alt hotkey
souffleur

# 3b. ...or just tail the live captions to the console
souffleur capture
```

> All commands below use the installed `souffleur` entry point. If you didn't
> install it, replace `souffleur` with `python -m souffleur`.

## Use

Souffleur's main mode is the **daemon** (`souffleur`): it watches the
Teams transcript in the background and, on **one global hotkey**, pastes the
latest lines into the **Clawpilot / Microsoft Scout** chat and presses Send — so
you get a live, in-context answer while you talk.

### 1. Prepare

1. Open **Clawpilot** and prime the chat once with your instruction/persona, e.g.
   *"You are an expert interviewer; read the transcript and suggest the best next
   answer to the latest question."*
2. Join your Teams meeting and **turn on live captions**
   (**More (...) → Language and speech → Turn on live captions**).

### 2. Start the daemon

```powershell
souffleur
```

This launches Clawpilot if it isn't open, brings it to the front, starts the
transcript reader, registers the hotkey, and prints
`ready. Press the hotkey to send the transcript.`

### 3. Press the hotkey

While you speak/listen, press **Win+Ctrl+Alt** whenever you want an answer (all
three modifiers sit in the bottom-left corner — a one-handed chord that avoids
the Ctrl+Shift language switcher).

On each press, the **new** transcript lines since your last press are pasted into
the current Clawpilot chat and sent, and the answer streams back in Clawpilot.
The console logs `⌨ hotkey detected` for every press (followed by
`[sent N lines …]`, or a reason such as `[nothing new to send]`). Press
**Ctrl+C** to quit.

See [Souffleur daemon](#souffleur-daemon-the-run-mode--transcript--clawpilot-on-one-hotkey)
for behaviour notes and the `config.toml` reference (hotkey, send template, etc.).

## Advanced use

The other subcommands are for diagnostics or plain transcript capture (no
daemon, no Clawpilot):

```powershell
souffleur doctor          # one-line readiness check
souffleur discover        # lists windows + caption region
souffleur discover --tree # dump the meeting window UIA subtree
souffleur capture         # tail live captions to the console
```

`doctor` should report `Caption region : OK`; `discover` should report
`Found: ... Name='Live Captions'` and preview a few lines.

With `capture`, finalized caption lines (`[HH:MM:SS] Speaker: text`) stream to
**stdout** and status/heartbeat messages to **stderr**, so you can redirect just
the transcript. If captions aren't on yet, it waits (forever by default) and
starts as soon as they appear; press **Ctrl+C** to stop.

```powershell
souffleur capture > transcript.txt
```

### Options (`capture`)

| Flag | Default | Meaning |
|------|---------|---------|
| `--interval` | `0.25` | Polling interval in seconds. |
| `--timeout` | `0` | Seconds to wait for captions before giving up (`0` = wait forever). |
| `--show-live` | off | Also show the in-progress (not-yet-final) line on stderr. |
| `--container-aid` | auto | Force a caption container by AutomationId. |
| `--container-name` | auto | Force a caption container by Name substring. |
| `--depth` | `40` | Max UIA search depth. |

## How it works

New Teams is a WebView2 (Chromium) app, so its DOM is exposed through UI
Automation. Souffleur:

1. Finds the Teams **meeting** window.
2. Locates the **`Live Captions`** region (a `fui-Flex` group). Detection is
   **language-independent**: if the Teams UI language localizes the region's
   label, Souffleur falls back to the lowest common ancestor of the caption
   elements instead of matching the English word "caption".
3. Reads each caption entry — a `fui-ChatMessageCompact__body` element whose two
   children are `[author, caption text]`.
4. Treats the last visible line as "live" and finalizes earlier lines once a
   newer line appears below them (handles the in-place-growing last caption and
   list scrolling), de-duplicating as it goes.
5. **Re-acquires** the region automatically if reads dry up (a caption-language
   change or panel toggle rebuilds the subtree), and drops back to a waiting
   state — without losing transcript continuity — if the meeting goes away.

If a future Teams update changes these element names, run
`souffleur discover --tree` to inspect the current tree and pass
`--container-name` / `--container-aid` explicitly.

## Souffleur daemon (the `run` mode) — transcript → Clawpilot on one hotkey

The `run` mode (implemented in `souffleur/daemon.py`) turns Souffleur into a live
"souffleur": it reads the Teams
transcript in the background and, on **one global hotkey**, pastes the latest
transcript into the **current chat** of the **Clawpilot / Microsoft Scout**
desktop app and presses Send — so you get a live, in-context answer while you
talk.

You prime the Clawpilot chat once with your instruction/persona
(e.g. *"You are an expert interviewer; read the transcript and suggest the
best next answer to the latest question."*). Every hotkey press then feeds it
fresh context. Refine by simply typing in Clawpilot as usual.

### Run

```powershell
souffleur                          # default: runs the daemon (same as `run`)
souffleur run                      # explicit; uses ./config.toml (auto-created on first run)
souffleur run -c my.toml           # custom config
python -m souffleur                # equivalent, without installing the command
python -m souffleur.daemon         # run the daemon module directly
```

On start it launches Clawpilot (if not already open), brings it to the front,
starts the transcript reader, and registers the hotkey. Then:

1. Open/prime your Clawpilot chat.
2. Join a Teams meeting with **Live Captions ON**.
3. Speak / listen. When you want an answer, press the hotkey
   (**Win+Ctrl+Alt** by default — all three modifiers sit in the bottom-left
   corner, so it's a one-handed chord that avoids the Ctrl+Shift language
   switcher).
4. The latest transcript (only the *new* lines since your last press) is pasted
   into the current chat and sent; the answer streams in Clawpilot.

Finalized transcript lines and status messages (`⌨ hotkey detected`,
`[sent N lines / M chars]`, `[nothing new to send]`,
`[skipped: Clawpilot is generating]`, `[clawpilot relaunched]`) print to the
console in real time. The `⌨ hotkey detected` line confirms every press was
seen — if a send doesn't follow, the status line says why (e.g. nothing new, or
Clawpilot busy). Press **Ctrl+C** to quit.

### Behaviour notes

- **Color-coded console**: each speaker is shown in a stable color derived from a
  hash of their name (the same person is always the same color, in both the
  daemon and `capture` output), while status messages are gray and errors red.
  Coloring is automatic on a terminal and is turned off when output is
  redirected to a file or when the `NO_COLOR` environment variable is set.
- **Delta by default**: each press sends only lines added since the previous
  press (the whole transcript on the first press). Set `send.mode = "full"` to
  always send everything.
- **Real-time / partial capture**: the in-progress caption line (a paragraph
  that is still growing before Teams finalizes it) is shown live on the console
  (a `… <text>` line that updates in place) and is included in each send by
  default (`send.include_live = true`) — so pressing the hotkey mid-sentence
  still captures what's been said so far.
- **Won't interrupt**: if Clawpilot is mid-generation (the action button shows
  *Stop*), the press is skipped rather than cancelling the answer.
- **Clipboard-safe**: text is sent via clipboard paste and your previous
  clipboard contents are restored.
- **Reliable detection**: the hotkey is read by polling the physical keyboard
  state (`GetAsyncKeyState`) every 25 ms, with a short sliding window so a
  one-handed *rolling* press (the three modifiers landing a few ms apart) fires
  on the first try — no need to hit all keys in the same instant. This avoids
  the dropped-keystroke problem of low-level keyboard hooks. Tune
  `hotkey.window` if needed.
- **Watchdog**: if Clawpilot is closed, it is relaunched automatically.
- The Clawpilot window is brought to the foreground when sending (the app's
  Electron input can't be filled in the background). This is fine when you
  share a *window* rather than your full screen.

### Configuration (`config.toml`)

Auto-created with defaults on first run. Key settings:

| Section / key | Default | Meaning |
|---|---|---|
| `hotkey.combo` | `win+ctrl+alt` | Global trigger. Shorthand: `+`-separated modifiers/keys (`win+ctrl+alt`, `ctrl+f8`, `win+ctrl+alt+z`). `win` = Windows key. Avoid Ctrl+Shift (language switcher). A modifier-only chord fires once per hold (release one key to re-fire). |
| `hotkey.window` | `0.25` | Seconds tolerance for a "rolling" press — how far apart the chord keys may land and still count as one press. Raise if presses get missed; lower to reduce accidental triggers. |
| `clawpilot.exe` | `C:/Program Files (x86)/Clawpilot/Clawpilot.exe` | App to launch if not running. |
| `clawpilot.window_title` | `Clawpilot` | Window name used to find the app. |
| `clawpilot.foreground_on_start` | `true` | Bring Clawpilot to front at startup. |
| `send.mode` | `delta` | `delta` (new lines only) or `full`. |
| `send.max_chars` | `12000` | Cap; oldest lines trimmed, newest kept. |
| `send.include_live` | `true` | Include the in-progress (not-yet-finalized) caption line so long paragraphs aren't missed mid-sentence. |
| `send.template` | `Here is a transcript of the meeting (or follow-up):\n'''\n{payload}\n'''\nFind the latest question(s) and answer as an expert.` | `{payload}` is the transcript. |
| `send.restore_clipboard` | `true` | Restore prior clipboard after pasting. |
| `capture.interval` | `0.5` | Transcript polling interval (s). |
| `watchdog.interval` | `5.0` | Clawpilot alive-check interval (s). |

## Files

The code is a Python package (`souffleur/`) exposing the `souffleur` command.

| File | Purpose |
|------|---------|
| `souffleur/__init__.py` | Package init; exposes `main` and `__version__`. |
| `souffleur/__main__.py` | Enables `python -m souffleur`. |
| `souffleur/cli.py` | Main CLI entry: `run` (daemon, default), `capture`, `discover`, `doctor`. |
| `souffleur/teams_ui.py` | Reusable transcript-capture core + `TranscriptReader` background thread. |
| `souffleur/scout.py` | `ScoutWriter` — drives Clawpilot/Scout via UI Automation (paste + Send). |
| `souffleur/colors.py` | Stable per-speaker terminal colors + system/error coloring. |
| `souffleur/daemon.py` | The Souffleur daemon (the `run` mode): transcript → Clawpilot on a global hotkey. |
| `pyproject.toml` | Packaging + `souffleur` console-script entry point. |
| `config.toml` | Prompter configuration (auto-created in the working directory). |
| `requirements.txt` | Runtime dependency (`uiautomation`); mirrors `pyproject.toml`. |

## Limitations

- Requires Live Captions to be turned on (Souffleur reads Teams' captions; it does
  not transcribe audio itself). For a Teams-independent alternative, capture
  loopback audio (WASAPI) and run a local STT model such as whisper.cpp.
- Caption accuracy is whatever Teams produces.
- Element names are Teams-version dependent (see "How it works").
