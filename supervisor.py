#!/usr/bin/env python3
"""
Honest crash-recovery supervisor for the telemetry HUD.

This restarts the HUD only when it *exits* (e.g. it crashed). It is a
reliability tool, not an anti-tamper mechanism:

* Ctrl+C (SIGINT) / SIGTERM shuts the supervisor AND the child down cleanly
  and does NOT respawn.
* Restarts use exponential backoff and a cap within a rolling time window, so
  a HUD that crashes on startup won't spin forever — the supervisor gives up
  and tells you to look at the error.

Usage
-----
    python supervisor.py --page https://www.google.com
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Crash-recovery supervisor for the HUD.")
    parser.add_argument("--page", default="https://www.google.com",
                        help="Page to pass through to the HUD (default: "
                             "https://www.google.com)")
    parser.add_argument("--max-restarts", type=int, default=5,
                        help="Max restarts within --window seconds before giving up.")
    parser.add_argument("--window", type=float, default=60.0,
                        help="Rolling window (seconds) for the restart cap.")
    args = parser.parse_args()

    hud_path = Path(__file__).with_name("hud.py")
    cmd = [sys.executable, str(hud_path), "--page", args.page]

    state = {"child": None, "stopping": False}

    def shutdown(signum, _frame) -> None:
        state["stopping"] = True
        child = state["child"]
        if child is not None and child.poll() is None:
            child.terminate()
        print(f"\n[supervisor] signal {signum} — shutting HUD down, not restarting.")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    restart_times: list[float] = []

    while not state["stopping"]:
        print(f"[supervisor] launching HUD: {' '.join(cmd)}")
        child = subprocess.Popen(cmd)
        state["child"] = child
        code = child.wait()

        if state["stopping"]:
            break

        now = time.monotonic()
        restart_times = [t for t in restart_times if now - t < args.window]
        restart_times.append(now)
        print(f"[supervisor] HUD exited with code {code}.")

        if len(restart_times) > args.max_restarts:
            print(f"[supervisor] HUD exited {len(restart_times)} times in "
                  f"{args.window:.0f}s — giving up. Check the HUD output for errors.")
            sys.exit(1)

        backoff = min(2 ** (len(restart_times) - 1), 15)
        print(f"[supervisor] restarting in {backoff}s … (Ctrl+C to stop)")
        for _ in range(int(backoff * 10)):
            if state["stopping"]:
                break
            time.sleep(0.1)

    # Make sure the child is really gone on the way out.
    child = state["child"]
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
    print("[supervisor] done.")


if __name__ == "__main__":
    main()
