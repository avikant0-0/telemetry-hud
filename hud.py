#!/usr/bin/env python3
"""
Telemetry HUD — a borderless, always-on-top overlay that embeds a web page
(https://www.google.com by default).

The overlay starts interactive: clicks and keystrokes go to the page. A
system-wide hotkey (Ctrl+Alt+H by default) toggles click-through mode, in
which mouse and keyboard input pass through to whatever is underneath.

Design notes
------------
* The window is frameless and always-on-top, and it never appears in the
  taskbar or Alt+Tab (`Tool` hint plus `WS_EX_TOOLWINDOW`).
* While click-through, the window ignores mouse input and cannot take focus.
  On Windows this uses `Qt.WindowTransparentForInput`, `WS_EX_TRANSPARENT`,
  and `WS_EX_NOACTIVATE`; the hotkey flips those native styles and the Qt
  hit-test attributes at runtime.
* On Windows the pixels are also excluded from screen capture via
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` (Windows 10 2004+).
  This is a visual privacy measure only: the process, its window handle, and
  its title remain fully visible to the OS and to you. Pass
  `--capture-exclusion off` to disable it, or `monitor` for the legacy
  black-rectangle behaviour on older builds.
* The embedded page is rendered by QtWebEngine, which runs its own browser
  engine process.
* The global hotkey is registered with `RegisterHotKey`, so it works no
  matter which application currently has focus.

Usage
-----
    python hud.py --page https://www.google.com
"""

from __future__ import annotations

import argparse
import signal
import sys

from PyQt6.QtCore import QEvent, Qt, QTimer, QUrl
from PyQt6.QtGui import QFont, QGuiApplication, QShowEvent
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "The 'PyQt6-WebEngine' package is required for the embedded page. "
        "Install with:\n"
        "    pip install PyQt6-WebEngine\n"
    )
    raise

from hotkey import GlobalHotkey, hotkey_label
from win_stealth import (
    CAPTURE_MODES,
    StealthReport,
    apply_window_stealth,
    set_input_transparency,
)


class OverlayHUD(QWidget):
    """The on-screen overlay: an embedded browser with a click-through toggle."""

    def __init__(
        self,
        page_url: str = "https://www.google.com",
        width: int = 520,
        height: int = 680,
        capture_exclusion: str = "exclude",
        hotkey: str = "ctrl+alt+h",
        start_click_through: bool = False,
    ) -> None:
        super().__init__()
        self._page_url = page_url
        self._fixed_width = width
        self._fixed_height = height
        self._capture_exclusion = capture_exclusion
        self._interactive = not start_click_through
        self._stealth_hwnd: int | None = None
        self.stealth_report: StealthReport | None = None
        self._hotkey = GlobalHotkey(hotkey, self)
        self._hotkey.activated.connect(self.toggle_interactive)
        self._build_window()
        self._build_ui()

    # -- window setup -------------------------------------------------------

    def _build_window(self) -> None:
        self.setWindowTitle("Telemetry HUD")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        # Translucent background so only our rounded panel is painted.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Showing the window must not activate / focus it.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_stealth()
        self._register_hotkey()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WinIdChange:
            self._stealth_hwnd = None
            self._hotkey.detach()
            if self.isVisible():
                QTimer.singleShot(0, self._apply_stealth)
                QTimer.singleShot(0, self._register_hotkey)
        return super().event(event)

    def _apply_stealth(self) -> None:
        self._sync_window_flags()
        try:
            hwnd = int(self.winId())
            if hwnd == self._stealth_hwnd:
                return
            report = apply_window_stealth(
                hwnd,
                self._capture_exclusion,
                input_transparent=not self._interactive,
            )
        except Exception as exc:  # noqa: BLE001 — stealth is best-effort
            print(f"[hud] window stealth failed: {exc}", file=sys.stderr)
            return
        self._stealth_hwnd = hwnd
        self.stealth_report = report
        for message in report.messages:
            print(f"[hud] {message}", file=sys.stderr)

    def _register_hotkey(self) -> None:
        if not self._hotkey.attach(int(self.winId())):
            print(
                f"[hud] global hotkey {hotkey_label(self._hotkey.spec)} "
                f"unavailable: {self._hotkey.register_error}",
                file=sys.stderr,
            )
        self._update_mode_ui()

    def _build_ui(self) -> None:
        self.setObjectName("hudPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(self._fixed_width, self._fixed_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._mode_label = QLabel()
        self._mode_label.setFont(QFont("Menlo", 8))

        self._status_label = QLabel(f"loading {self._page_url} …")
        self._status_label.setFont(QFont("Menlo", 8))
        self._status_label.setStyleSheet("color: rgba(150, 160, 175, 230);")

        self._web_view = QWebEngineView(self)
        self._web_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._web_view.loadStarted.connect(self._on_load_started)
        self._web_view.loadProgress.connect(self._on_load_progress)
        self._web_view.loadFinished.connect(self._on_load_finished)

        layout.addWidget(self._mode_label)
        layout.addWidget(self._status_label)
        layout.addWidget(self._web_view, 1)

        self.set_interactive(self._interactive, apply_native=False)
        self._web_view.setUrl(QUrl.fromUserInput(self._page_url))

    def position_top_right(self, margin: int = 24) -> None:
        primary = QGuiApplication.primaryScreen()
        if primary is None:
            return
        screen = primary.availableGeometry()
        self.adjustSize()
        x = screen.right() - self.width() - margin
        y = screen.top() + margin
        self.move(x, y)

    # -- input mode ---------------------------------------------------------

    def toggle_interactive(self) -> None:
        self.set_interactive(not self._interactive)

    def _sync_window_flags(self) -> None:
        """Keep Qt's window flags in sync with the current input mode.

        QWidget's flags are fixed at creation; the live QWindow flags are what
        Qt consults when deciding whether a click may activate/focus the
        window, so they must be updated along with the native styles.
        """
        window = self.windowHandle()
        if window is None:
            return
        transparent = not self._interactive
        window.setFlag(Qt.WindowType.WindowTransparentForInput, transparent)
        window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, transparent)

    def set_interactive(
        self, interactive: bool, apply_native: bool = True
    ) -> None:
        self._interactive = bool(interactive)
        transparent = not self._interactive

        self._sync_window_flags()
        if apply_native:
            messages: tuple[str, ...] = ()
            try:
                ok, messages = set_input_transparency(
                    int(self.winId()), transparent
                )
            except Exception as exc:  # noqa: BLE001 — best-effort toggle
                ok = False
                print(f"[hud] failed to change input mode: {exc}",
                      file=sys.stderr)
            if not ok:
                for message in messages:
                    print(f"[hud] {message}", file=sys.stderr)

        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent
        )
        self._web_view.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent
        )
        policy = (
            Qt.FocusPolicy.NoFocus if transparent
            else Qt.FocusPolicy.StrongFocus
        )
        self.setFocusPolicy(policy)
        self._web_view.setFocusPolicy(policy)
        if transparent:
            self._web_view.clearFocus()
            self.clearFocus()
        self._update_mode_ui()

    def _update_mode_ui(self) -> None:
        hint = hotkey_label(self._hotkey.spec)
        if self._hotkey.register_error:
            hint = f"{hint} unavailable"
        if self._interactive:
            self._mode_label.setText(f"interactive · {hint} to lock")
            mode_color = "rgba(120, 190, 255, 255)"
            border = "rgba(90, 170, 255, 200)"
        else:
            self._mode_label.setText(f"click-through · {hint} to interact")
            mode_color = "rgba(150, 160, 175, 230)"
            border = "rgba(255, 255, 255, 20)"
        self._mode_label.setStyleSheet(f"color: {mode_color};")
        self.setStyleSheet(
            "#hudPanel { background-color: rgba(18, 20, 26, 190); "
            f"border-radius: 10px; border: 1px solid {border}; }}"
        )

    # -- slots (run on the GUI thread) -------------------------------------

    def _on_load_started(self) -> None:
        self._status_label.setText(f"loading {self._current_url()} …")

    def _on_load_progress(self, progress: int) -> None:
        self._status_label.setText(
            f"loading {self._current_url()} … {progress}%"
        )

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self._status_label.setText(f"loaded {self._current_url()}")
        else:
            self._status_label.setText(f"failed to load {self._page_url}")

    def _current_url(self) -> str:
        url = self._web_view.url().toString()
        return url or self._page_url

    def shutdown(self) -> None:
        self._hotkey.detach()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Borderless web page overlay with a click-through hotkey."
    )
    parser.add_argument("--page", default="https://www.google.com",
                        help="Page to embed in the overlay "
                             "(default: https://www.google.com)")
    parser.add_argument("--width", type=int, default=520,
                        help="Overlay width in pixels (default: 520)")
    parser.add_argument("--height", type=int, default=680,
                        help="Overlay height in pixels (default: 680)")
    parser.add_argument("--opacity", type=float, default=1.0,
                        help="Window opacity 0.0–1.0 (default: 1.0)")
    parser.add_argument("--hotkey", default="ctrl+alt+h",
                        help="Global hotkey that toggles click-through "
                             "(default: ctrl+alt+h)")
    parser.add_argument("--start-click-through", action="store_true",
                        help="Start in click-through mode instead of "
                             "interactive mode")
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

    try:
        hud = OverlayHUD(
            args.page,
            width=args.width,
            height=args.height,
            capture_exclusion=args.capture_exclusion,
            hotkey=args.hotkey,
            start_click_through=args.start_click_through,
        )
    except ValueError as exc:
        parser.error(str(exc))
    hud.setWindowOpacity(max(0.1, min(1.0, args.opacity)))
    hud.show()
    hud.position_top_right()

    app.aboutToQuit.connect(hud.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
