#!/usr/bin/env python3
"""
Telemetry HUD — a borderless, click-through, always-on-top overlay that
displays real-time text pushed from a WebSocket server.

Design notes
------------
* The window is frameless, translucent, always-on-top, and *transparent to
  mouse input*, so it sits over whatever you're working in without stealing
  focus or intercepting clicks. It identifies itself normally to the OS —
  it does not hide from the taskbar switcher beyond the usual `Tool` hint,
  and it runs under its own name.
* On Windows the pixels are also excluded from screen capture via
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` (Windows 10 2004+).
  This is a visual privacy measure only: the process, its window handle, and
  its title remain fully visible to the OS and to you. Pass
  `--capture-exclusion off` to disable it, or `monitor` for the legacy
  black-rectangle behaviour on older builds.
* The WebSocket client runs its own asyncio loop on a background thread and
  hands messages to the GUI thread through a Qt signal. (Qt widgets must only
  ever be touched from the main thread; queued signals do that marshalling
  for us.)
* It auto-reconnects with capped exponential backoff if the server drops.

Usage
-----
    python hud.py --url ws://localhost:8765
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import threading

from PyQt6.QtCore import QEvent, Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QGuiApplication, QShowEvent
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from win_stealth import CAPTURE_MODES, StealthReport, apply_window_stealth

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "The 'websockets' package is required. Install with:\n"
        "    pip install websockets\n"
    )
    raise


class WebSocketClient(QObject):
    """Runs an asyncio WebSocket client on a background thread.

    Emits `message_received` for each text frame and `status_changed` for
    connection-state changes. Both cross back to the GUI thread safely via
    Qt's queued signal/slot delivery.
    """

    message_received = pyqtSignal(str)
    status_changed = pyqtSignal(str)

    def __init__(self, url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="ws-client", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # -- background thread --------------------------------------------------

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self._listen())
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            self._loop.close()

    async def _listen(self) -> None:
        backoff = 1.0
        max_backoff = 30.0
        while not self._stop.is_set():
            try:
                self.status_changed.emit(f"connecting to {self._url} …")
                async with websockets.connect(self._url, ping_interval=20) as ws:
                    self.status_changed.emit("connected")
                    backoff = 1.0
                    async for message in ws:
                        if isinstance(message, (bytes, bytearray)):
                            message = message.decode("utf-8", "replace")
                        self.message_received.emit(message)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — surface any connect error
                if self._stop.is_set():
                    break
                self.status_changed.emit(
                    f"disconnected ({exc!s}); retrying in {backoff:.0f}s"
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    break
                backoff = min(backoff * 2, max_backoff)


class OverlayHUD(QWidget):
    """The on-screen overlay: a translucent, click-through log tail."""

    def __init__(
        self,
        url: str,
        max_lines: int = 14,
        width: int = 420,
        capture_exclusion: str = "exclude",
    ) -> None:
        super().__init__()
        self._lines: list[str] = []
        self._max_lines = max_lines
        self._fixed_width = width
        self._capture_exclusion = capture_exclusion
        self._stealth_hwnd: int | None = None
        self.stealth_report: StealthReport | None = None
        self._build_window()
        self._build_ui()

        self._client = WebSocketClient(url)
        self._client.message_received.connect(self.append_message)
        self._client.status_changed.connect(self.set_status)
        self._client.start()

    # -- window setup -------------------------------------------------------

    def _build_window(self) -> None:
        self.setWindowTitle("Telemetry HUD")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput  # <- click-through
        )
        # Translucent background so only our rounded panel is painted.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Showing the window must not activate / focus it.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_stealth()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WinIdChange:
            self._stealth_hwnd = None
            if self.isVisible():
                QTimer.singleShot(0, self._apply_stealth)
        return super().event(event)

    def _apply_stealth(self) -> None:
        try:
            hwnd = int(self.winId())
            if hwnd == self._stealth_hwnd:
                return
            report = apply_window_stealth(hwnd, self._capture_exclusion)
        except Exception as exc:  # noqa: BLE001 — stealth is best-effort
            print(f"[hud] window stealth failed: {exc}", file=sys.stderr)
            return
        self._stealth_hwnd = hwnd
        self.stealth_report = report
        for message in report.messages:
            print(f"[hud] {message}", file=sys.stderr)

    def _build_ui(self) -> None:
        self.setFixedWidth(self._fixed_width)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        self._status_label = QLabel("starting…")
        self._status_label.setFont(QFont("Menlo", 8))
        self._status_label.setStyleSheet("color: rgba(150, 160, 175, 230);")

        self._message_label = QLabel("")
        self._message_label.setFont(QFont("Menlo", 11))
        self._message_label.setStyleSheet("color: rgba(232, 236, 242, 255);")
        self._message_label.setWordWrap(True)
        self._message_label.setTextFormat(Qt.TextFormat.PlainText)
        self._message_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction
        )

        layout.addWidget(self._status_label)
        layout.addWidget(self._message_label)

        self.setStyleSheet(
            "QWidget { background-color: rgba(18, 20, 26, 190); "
            "border-radius: 10px; }"
        )

    def position_top_right(self, margin: int = 24) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        x = screen.right() - self.width() - margin
        y = screen.top() + margin
        self.move(x, y)

    # -- slots (run on the GUI thread) -------------------------------------

    def append_message(self, text: str) -> None:
        for line in (text.splitlines() or [""]):
            self._lines.append(line)
        self._lines = self._lines[-self._max_lines:]
        self._message_label.setText("\n".join(self._lines))
        self.adjustSize()
        self.position_top_right()

    def set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def shutdown(self) -> None:
        self._client.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time telemetry HUD overlay.")
    parser.add_argument("--url", default="ws://localhost:8765",
                        help="WebSocket server URL (default: ws://localhost:8765)")
    parser.add_argument("--opacity", type=float, default=1.0,
                        help="Window opacity 0.0–1.0 (default: 1.0)")
    parser.add_argument("--max-lines", type=int, default=14,
                        help="How many recent lines to keep on screen")
    parser.add_argument("--capture-exclusion", choices=CAPTURE_MODES,
                        default="exclude",
                        help="Screen-capture handling on Windows: 'exclude' "
                             "(omit from DWM captures, Win10 2004+), 'monitor' "
                             "(black rectangle in captures), or 'off' "
                             "(default: exclude)")
    args = parser.parse_args()

    app = QApplication(sys.argv)

    # Let Ctrl+C in the terminal close the HUD cleanly. A no-op timer keeps
    # the Python interpreter ticking so the SIGINT handler can actually fire
    # while Qt owns the event loop.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.start(200)
    keepalive.timeout.connect(lambda: None)

    hud = OverlayHUD(
        args.url,
        max_lines=args.max_lines,
        capture_exclusion=args.capture_exclusion,
    )
    hud.setWindowOpacity(max(0.1, min(1.0, args.opacity)))
    hud.show()
    hud.position_top_right()

    app.aboutToQuit.connect(hud.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
