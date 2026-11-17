# Telemetry HUD

A borderless, translucent, always-on-top overlay that embeds a web page
(`https://www.google.com` by default). It starts **interactive**, so clicks and
keystrokes go to the page. Press the global hotkey (`Ctrl+Alt+X` by default) to
toggle **click-through** mode, where input passes straight through to whatever
is underneath. `Ctrl+Alt+C` clears (hides) the overlay completely (the same hotkey
brings it back), and `Ctrl+Alt+Arrow` nudges the overlay in any mode. In
click-through mode the window also dims to `--click-through-opacity`
(0.35 by default) so it stays out of the way. `Ctrl+Alt+S` takes a screenshot
of the main screen, copies it to the clipboard, and (by default) sends
`Ctrl+V` so it lands in whatever window has focus — for example, a Google AI
prompt box in your browser. Press `Ctrl+Alt+Q` or click the `✕` button on the
top header to quit the application cleanly. The overlay never steals focus while click-through
and stays out of the taskbar/Alt+Tab.

This is an honest utility: it runs under its own name, shows up normally to
the OS, and the supervisor only restarts the HUD if it *crashes* — it does not
resist you or your tools shutting it down.

## Files

| File             | What it does                                                        |
|------------------|---------------------------------------------------------------------|
| `hud.py`         | The overlay window + embedded QtWebEngine browser view.             |
| `hotkey.py`      | System-wide hotkeys (RegisterHotKey): click-through toggle, hide, movement, and screenshot. |
| `screenshot.py`  | Screen and page capture, clipboard copy, and synthesized Ctrl+V paste. |
| `win_stealth.py` | Windows capture exclusion + input-mode window styles.               |
| `supervisor.py`  | Optional crash-recovery watchdog (relaunches the HUD if it exits).  |
| `mock_server.py` | Legacy WebSocket test source (no longer used by the overlay).       |
| `requirements.txt` | PyQt6, PyQt6-WebEngine, and websockets (for `mock_server.py`).    |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Try it

```bash
python hud.py
```

You should see a panel in the top-right corner showing Google. Click into it
and type normally; `Ctrl+Alt+X` locks it into click-through mode, and the same
hotkey unlocks it. `Ctrl+Alt+C` clears the overlay (makes it disappear entirely); press it
again to bring it back. `Ctrl+Alt+Arrow` moves the panel; it is clamped to the
virtual desktop so it cannot be nudged completely off-screen.

`Ctrl+Alt+S` screenshots the main screen. The image is copied to the clipboard, and a
moment later the HUD sends `Ctrl+V` to the focused window, so if your browser
(or the HUD itself) has a Google AI prompt focused, the screenshot appears
there immediately. Pass `--screenshot-paste clipboard` to copy only and paste
yourself. Pass `--screenshot-target hud` to screenshot the embedded page instead.

Point it elsewhere with `--page`:

```bash
python hud.py --page https://example.com
```

Stop with `Ctrl+C`.

## Screenshots

`Ctrl+Alt+S` (or `--screenshot-hotkey`) grabs the primary/main screen and puts it
on the system clipboard as an image. Because the HUD is excluded from screen
capture by default (`--capture-exclusion exclude`), the HUD overlay itself is
omitted from the screenshot and does not block whatever is on your screen.

If you instead wish to capture only the HUD's embedded browser view, pass
`--screenshot-target hud`.

With `--screenshot-paste auto` (the default) the HUD waits ~250 ms for you to
release the hotkey, then synthesizes `Ctrl+V` with `SendInput`. Whichever window
has focus receives the paste — for example a Google AI / Gemini prompt box in
your browser, or the HUD's own page if it has focus. Use
`--screenshot-paste clipboard` to only copy the image and paste it yourself.

Auto-paste is Windows-only and follows the usual `SendInput` rules: it cannot
inject into an elevated (administrator) window from a non-elevated HUD. The
image is still on the clipboard in that case, so paste manually with `Ctrl+V`.

## Run with crash recovery

```bash
python supervisor.py --page https://www.google.com
```

`Ctrl+C` stops the supervisor and the HUD together (no respawn). If the HUD
crashes repeatedly (default: more than 5 times in 60s) the supervisor gives up
and prints why, instead of spinning forever.

## Options

`hud.py`:
- `--page` — page to embed (default `https://www.google.com`)
- `--width` — overlay width in pixels (default 520)
- `--height` — overlay height in pixels (default 680)
- `--opacity` — window opacity in interactive mode, 0.1–1.0 (default 1.0)
- `--click-through-opacity` — window opacity in click-through mode, 0.1–1.0
  (default 0.35)
- `--move-step` — pixels per `Ctrl+Alt+Arrow` nudge (default 40)
- `--hotkey` — global click-through/translucent toggle (default `ctrl+alt+x`)
- `--hide-hotkey` — global clear/hide toggle (default `ctrl+alt+c`)
- `--quit-hotkey` — global exit hotkey (default `ctrl+alt+q`)
- `--screenshot-hotkey` — global screenshot hotkey (default `ctrl+alt+s`)
- `--screenshot-target` — `screen` (default) captures the main screen; `hud` captures the embedded page
- `--screenshot-paste` — `auto` (default) copies the screenshot and sends
  `Ctrl+V` to the focused window; `clipboard` only copies it
- `--start-click-through` — start locked/click-through instead of interactive
- `--capture-exclusion` — `exclude` (default), `monitor`, or `off` (see below)

`supervisor.py`:
- `--page` — passed through to the HUD
- `--max-restarts` / `--window` — the restart cap and its rolling window

## Visual stealth (Windows)

After the window is shown, the HUD applies two independent stealth layers.

**Capture exclusion.** `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
keeps the window out of DWM-based capture paths: Snipping Tool, Xbox Game
Bar, Windows.Graphics.Capture apps, and most window/display recorders on
Windows 10 2004+ (build 19041). The HUD still renders normally on your
physical monitor.

**Input modes.** The global hotkey toggles between two native input modes.
Interactive mode removes `WS_EX_TRANSPARENT`/`WS_EX_NOACTIVATE` and lets the
window activate, so you can click links, scroll, and type in the page.
Click-through mode adds `WS_EX_TRANSPARENT | WS_EX_NOACTIVATE` and clears the
widget hit-test/focus flags, so input goes to the app underneath. Both modes
keep `WS_EX_TOOLWINDOW`, so the HUD never appears in the taskbar or Alt+Tab.
Because it is a tool window with `WA_ShowWithoutActivating`, merely launching
it does not steal focus in either mode.

| `--capture-exclusion` | Behaviour |
|-----------------------|-----------|
| `exclude` (default) | Window is absent from DWM-based captures. Needs Win10 2004+. |
| `monitor` | Window appears as a black rectangle in captures (legacy fallback). |
| `off` | No display affinity is set; the HUD is captured like any window. |

What this is **not**: a hardware or DRM guarantee. A capture card, a phone
camera, another machine recording the screen, and some legacy capture APIs
can still see the pixels. The process, window handle, and title remain fully
visible to the OS (Task Manager, Alt+Tab process lists, monitoring tools) —
this hides pixels from capture, not the app from the system. Treat it as
stream/screenshot privacy, not a secrecy boundary. On non-Windows platforms
the stealth module is a no-op and only Qt's cross-platform flags apply.

## Notes on behaviour

- **Click-through** is toggled by the global hotkey. `hud.py` updates both the
  Qt window flags (`WindowTransparentForInput`, `WindowDoesNotAcceptFocus`)
  and the native `WS_EX_TRANSPARENT`/`WS_EX_NOACTIVATE` styles at runtime, so
  no window recreation or flicker is involved. The hotkey itself is a Windows
  `RegisterHotKey` binding, so it works while another app has focus.
- **Movement** uses four more `RegisterHotKey` bindings (`Ctrl+Alt+Arrow`) and
  works in both modes, so a locked, click-through HUD can still be repositioned.
  Each move is clamped to the virtual desktop's bounds.
- **Hide/show** uses another `RegisterHotKey` binding (`Ctrl+Alt+C`). Hiding
  removes the window from the screen; the binding stays registered, so the same
  hotkey brings it back with its previous position and mode.
- **Screenshots** use another `RegisterHotKey` binding (`Ctrl+Alt+S`). The page
  is rendered to a `QPixmap` in-process and placed on the clipboard, which
  bypasses the window's capture exclusion. Auto-paste sends `Ctrl+V` to the
  focused window via `SendInput` after a short delay.
- **Click-through dimming** applies `--click-through-opacity` with
  `setWindowOpacity`, and restores `--opacity` when you switch back to
  interactive mode.
- **No focus stealing on launch** comes from `WA_ShowWithoutActivating` and
  the `Tool` window flag. In interactive mode a click focuses the page, as
  usual.
- **Capture exclusion** comes from `SetWindowDisplayAffinity` and is
  best-effort; see “Visual stealth” above.
- **Translucent panel** comes from `WA_TranslucentBackground` plus the
  rounded-rect stylesheet.
