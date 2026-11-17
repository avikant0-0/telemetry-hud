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

`supervisor.py`:
- `--url` — passed through to the HUD
- `--max-restarts` / `--window` — the restart cap and its rolling window

## Wiring it to real data

Point `--url` at any WebSocket server that sends text frames; each frame is
appended as a new line (multi-line frames are split). For example, a small
`asyncio` producer that reads `journalctl -f`, `docker logs -f`, or your app's
metrics endpoint and forwards each line over `websockets.serve(...)` drops
straight in where `mock_server.py` is.

## Notes on behaviour

- **Click-through** comes from `Qt.WindowType.WindowTransparentForInput`. A
  side effect is that you can't drag the window with the mouse — it's
  positioned automatically (top-right). Change `position_top_right()` in
  `hud.py` if you want it elsewhere.
- **No focus stealing** comes from `WA_ShowWithoutActivating` plus the `Tool`
  window flag.
- **Translucent panel** comes from `WA_TranslucentBackground` plus the
  rounded-rect stylesheet.
