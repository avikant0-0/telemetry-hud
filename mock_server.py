#!/usr/bin/env python3
"""
Tiny test server for the telemetry HUD.

Pushes a timestamped, fake-metrics line once a second to every connected
client, so you can see the HUD working before you wire it to a real source.

Usage
-----
    python mock_server.py
    # then, in another terminal:
    python hud.py --url ws://localhost:8765
"""

from __future__ import annotations

import asyncio
import datetime
import random

import websockets


async def handler(ws) -> None:
    try:
        while True:
            now = datetime.datetime.now().strftime("%H:%M:%S")
            cpu = random.randint(5, 95)
            mem = random.randint(30, 80)
            rps = random.randint(50, 500)
            await ws.send(f"{now}  cpu={cpu:>2}%  mem={mem:>2}%  req/s={rps}")
            await asyncio.sleep(1)
    except websockets.ConnectionClosed:
        pass


async def main() -> None:
    async with websockets.serve(handler, "localhost", 8765):
        print("mock telemetry server listening on ws://localhost:8765 (Ctrl+C to stop)")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nmock server stopped.")
