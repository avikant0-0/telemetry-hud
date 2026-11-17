# Telemetry HUD

A borderless, translucent, always-on-top overlay that tails real-time text
pushed from a WebSocket server. It is **click-through** (transparent to mouse
input) and never steals focus, so it sits over your editor/terminal without
getting in the way.

This is an honest utility: it runs under its own name, shows up normally to
the OS, and the supervisor only restarts the HUD if it *crashes* — it does not
resist you or your tools shutting it down.

## Files

| File             | What it does                                                        |
|------------------|---------------------------------------------------------------------|
| `hud.py`         | The overlay window + background WebSocket client.                   |
| `win_stealth.py` | Windows capture exclusion + native input-transparency hardening.    |
| `supervisor.py`  | Optional crash-recovery watchdog (relaunches the HUD if it exits).  |
| `mock_server.py` | A 1-line-per-second fake telemetry source for testing.              |
| `requirements.txt` | PyQt6 + websockets.                                               |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Try it

In one terminal:

```bash
python mock_server.py
```

In another:

```bash
python hud.py --url ws://localhost:8765
```

You should see a panel in the top-right corner updating every second. Clicks
pass straight through it to whatever is underneath.

Stop either with `Ctrl+C`.

## Run with crash recovery

```bash
python supervisor.py --url ws://localhost:8765
```

`Ctrl+C` stops the supervisor and the HUD together (no respawn). If the HUD
crashes repeatedly (default: more than 5 times in 60s) the supervisor gives up
and prints why, instead of spinning forever.

## Options

`hud.py`:
- `--url` — WebSocket server URL (default `ws://localhost:8765`)
- `--opacity` — window opacity 0.1–1.0 (default 1.0)
- `--max-lines` — how many recent lines to keep on screen (default 14)
- `--capture-exclusion` — `exclude` (default), `monitor`, or `off` (see below)

`supervisor.py`:
- `--url` — passed through to the HUD
- `--max-restarts` / `--window` — the restart cap and its rolling window

## Visual stealth (Windows)

After the window is shown, the HUD applies two independent stealth layers.

**Capture exclusion.** `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
keeps the window out of DWM-based capture paths: Snipping Tool, Xbox Game
Bar, Windows.Graphics.Capture apps, and most window/display recorders on
Windows 10 2004+ (build 19041). The HUD still renders normally on your
physical monitor.

**Input hardening.** On top of `Qt.WindowTransparentForInput`, the native
window gets `WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`, plus
`WindowDoesNotAcceptFocus`, `WA_ShowWithoutActivating`, and
`WA_TransparentForMouseEvents`. Clicks go to the app underneath, clicking
never focuses the HUD, and it has no taskbar or Alt+Tab entry.

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

## Wiring it to real data

Point `--url` at any WebSocket server that sends text frames; each frame is
appended as a new line (multi-line frames are split). For example, a small
`asyncio` producer that reads `journalctl -f`, `docker logs -f`, or your app's
metrics endpoint and forwards each line over `websockets.serve(...)` drops
straight in where `mock_server.py` is.

## Notes on behaviour

- **Click-through** comes from `Qt.WindowType.WindowTransparentForInput`. On
  Windows, `win_stealth.py` additionally forces `WS_EX_TRANSPARENT` and
  `WS_EX_NOACTIVATE` on the native window. A side effect is that you can't
  drag the window with the mouse — it's positioned automatically (top-right).
  Change `position_top_right()` in `hud.py` if you want it elsewhere.
- **No focus stealing** comes from `WA_ShowWithoutActivating`,
  `WindowDoesNotAcceptFocus`, and the `Tool` window flag.
- **Capture exclusion** comes from `SetWindowDisplayAffinity` and is
  best-effort; see “Visual stealth” above.
- **Translucent panel** comes from `WA_TranslucentBackground` plus the
  rounded-rect stylesheet.
