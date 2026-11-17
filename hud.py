#!/usr/bin/env python3
"""
Telemetry HUD — a borderless, always-on-top overlay that embeds a web page
(https://www.google.com by default).

The overlay starts interactive: clicks and keystrokes go to the page. A
system-wide hotkey (Ctrl+Alt+X by default) toggles click-through mode, in
which mouse and keyboard input pass through to whatever is underneath.
Ctrl+Alt+C clears (hides) the overlay completely; the same hotkey brings it back.
Ctrl+Alt+Arrow keys nudge the overlay in any mode, and click-through mode
dims the window so it stays unobtrusive. Ctrl+Alt+S grabs a screenshot of the
main screen, copies it to the clipboard, and (by default) sends Ctrl+V so it
lands in whichever window has focus — e.g. a Google AI prompt box.
Ctrl+Alt+Q closes and exits the application cleanly.

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
* The global hotkeys are registered with `RegisterHotKey`, so they work no
  matter which application currently has focus.
* Screenshots capture the main display screen (or embedded page with
  `--screenshot-target hud`), copying it to the clipboard, and can optionally
  be pasted with a synthesized Ctrl+V. Under capture exclusion, the HUD window
  itself is omitted from the captured screen image.

Usage
-----
    python hud.py --page https://www.google.com
"""

from __future__ import annotations

import argparse
import signal
import sys

from PyQt6.QtCore import QEvent, QRect, Qt, QTimer, QUrl
from PyQt6.QtGui import QFont, QGuiApplication, QShowEvent
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def _log(message: str) -> None:
    """Best-effort diagnostics (windowed builds have no stderr)."""
    if sys.stderr is not None:
        print(f"[hud] {message}", file=sys.stderr)


try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover
    _log(
        "The 'PyQt6-WebEngine' package is required for the embedded page. "
        "Install with: pip install PyQt6-WebEngine"
    )
    raise

from hotkey import GlobalHotkey, hotkey_label
from screenshot import capture_screen, capture_widget, paste_shortcut
from win_stealth import (
    CAPTURE_MODES,
    StealthReport,
    apply_window_stealth,
    set_input_transparency,
)


class OverlayHUD(QWidget):
    """The on-screen overlay: an embedded browser with a click-through toggle."""

    _MOVE_BINDINGS = (
        ("ctrl+alt+left", -1, 0),
        ("ctrl+alt+up", 0, -1),
        ("ctrl+alt+right", 1, 0),
        ("ctrl+alt+down", 0, 1),
    )
    _MOVE_HOTKEY_ID = 0x4A32
    _HIDE_HOTKEY_ID = 0x4A36
    _SCREENSHOT_HOTKEY_ID = 0x4A37
    _QUIT_HOTKEY_ID = 0x4A38
    _PASTE_DELAY_MS = 250
    _STATUS_RESET_MS = 3000

    def __init__(
        self,
        page_url: str = "https://www.google.com",
        width: int = 520,
        height: int = 680,
        capture_exclusion: str = "exclude",
        hotkey: str = "ctrl+alt+x",
        hide_hotkey: str = "ctrl+alt+c",
        screenshot_hotkey: str = "ctrl+alt+s",
        quit_hotkey: str = "ctrl+alt+q",
        screenshot_paste: bool = True,
        screenshot_target: str = "screen",
        start_click_through: bool = False,
        click_through_opacity: float = 0.35,
        move_step: int = 40,
    ) -> None:
        super().__init__()
        self._page_url = page_url
        self._fixed_width = width
        self._fixed_height = height
        self._capture_exclusion = capture_exclusion
        self._screenshot_target = screenshot_target
        self._interactive = not start_click_through
        self._base_opacity = 1.0
        self._click_through_opacity = max(
            0.1, min(1.0, float(click_through_opacity))
        )
        self._move_step = max(1, int(move_step))
        self._stealth_hwnd: int | None = None
        self.stealth_report: StealthReport | None = None
        self._screenshot_paste = bool(screenshot_paste)
        self._page_loaded = False
        self._hotkey = GlobalHotkey(hotkey, self)
        self._hotkey.activated.connect(self.toggle_interactive)
        self._hide_hotkey = GlobalHotkey(
            hide_hotkey, self, hotkey_id=self._HIDE_HOTKEY_ID
        )
        self._hide_hotkey.activated.connect(self.toggle_visible)
        self._screenshot_hotkey = GlobalHotkey(
            screenshot_hotkey, self, hotkey_id=self._SCREENSHOT_HOTKEY_ID
        )
        self._screenshot_hotkey.activated.connect(self.copy_screenshot)
        self._quit_hotkey = GlobalHotkey(
            quit_hotkey, self, hotkey_id=self._QUIT_HOTKEY_ID
        )
        self._quit_hotkey.activated.connect(self._quit_app)
        self._screenshot_status_timer = QTimer(self)
        self._screenshot_status_timer.setSingleShot(True)
        self._screenshot_status_timer.timeout.connect(self._restore_status)
        self._move_hotkeys = self._build_move_hotkeys()
        self._build_window()
        self._build_ui()

    def _all_hotkeys(self) -> tuple[GlobalHotkey, ...]:
        return (
            self._hotkey,
            self._hide_hotkey,
            self._screenshot_hotkey,
            self._quit_hotkey,
            *self._move_hotkeys,
        )

    def _build_move_hotkeys(self) -> list[GlobalHotkey]:
        hotkeys: list[GlobalHotkey] = []
        for offset, (spec, dx, dy) in enumerate(self._MOVE_BINDINGS):
            hotkey = GlobalHotkey(
                spec, self, hotkey_id=self._MOVE_HOTKEY_ID + offset
            )
            hotkey.activated.connect(
                lambda _=False, dx=dx, dy=dy: self.move_by(
                    dx * self._move_step, dy * self._move_step
                )
            )
            hotkeys.append(hotkey)
        return hotkeys

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
            self._detach_hotkeys()
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
            _log(f"window stealth failed: {exc}")
            return
        self._stealth_hwnd = hwnd
        self.stealth_report = report
        for message in report.messages:
            _log(message)

    def _register_hotkey(self) -> None:
        hwnd = int(self.winId())
        for hotkey in self._all_hotkeys():
            if not hotkey.attach(hwnd):
                _log(
                    f"global hotkey {hotkey_label(hotkey.spec)} "
                    f"unavailable: {hotkey.register_error}"
                )
        self._update_mode_ui()

    def _detach_hotkeys(self) -> None:
        for hotkey in self._all_hotkeys():
            hotkey.detach()

    def _build_ui(self) -> None:
        self.setObjectName("hudPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(self._fixed_width, self._fixed_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._mode_label = QLabel()
        self._mode_label.setFont(QFont("Menlo", 8))

        self._close_btn = QPushButton("✕")
        self._close_btn.setToolTip("Close HUD")
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(180, 190, 205, 200); "
            "border: none; font-size: 11px; font-weight: bold; border-radius: 3px; } "
            "QPushButton:hover { color: #ff6b6b; background: rgba(255, 255, 255, 25); }"
        )
        self._close_btn.clicked.connect(self._quit_app)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)
        header_layout.addWidget(self._mode_label, 1)
        header_layout.addWidget(self._close_btn, 0)

        self._status_label = QLabel(f"loading {self._page_url} …")
        self._status_label.setFont(QFont("Menlo", 8))
        self._status_label.setStyleSheet("color: rgba(150, 160, 175, 230);")

        self._web_view = QWebEngineView(self)
        self._web_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._web_view.loadStarted.connect(self._on_load_started)
        self._web_view.loadProgress.connect(self._on_load_progress)
        self._web_view.loadFinished.connect(self._on_load_finished)

        layout.addLayout(header_layout)
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

    def _virtual_bounds(self) -> QRect | None:
        screens = QGuiApplication.screens()
        if not screens:
            return None
        bounds = screens[0].virtualGeometry()
        for screen in screens[1:]:
            bounds = bounds.united(screen.virtualGeometry())
        return bounds

    def move_by(self, dx: int, dy: int) -> None:
        x = self.x() + dx
        y = self.y() + dy
        bounds = self._virtual_bounds()
        if bounds is not None:
            x = max(bounds.left(), min(x, bounds.right() - self.width() + 1))
            y = max(bounds.top(), min(y, bounds.bottom() - self.height() + 1))
        self.move(x, y)

    # -- input mode ---------------------------------------------------------

    def toggle_interactive(self) -> None:
        self.set_interactive(not self._interactive)

    def toggle_visible(self) -> None:
        """Hide the overlay entirely, or bring it back if already hidden."""
        if self.isVisible():
            self.hide()
        else:
            self.show()

    # -- screenshots --------------------------------------------------------

    def copy_screenshot(self) -> None:
        """Copy the screen (or embedded page) to the clipboard, then optionally paste."""
        if self._screenshot_target == "hud":
            pixmap = capture_widget(self._web_view)
        else:
            pixmap = capture_screen()

        if pixmap is None:
            _log("screenshot skipped: unable to capture target")
            return
        QGuiApplication.clipboard().setPixmap(pixmap)
        size = f"{pixmap.width()}×{pixmap.height()}"
        target_name = "screen" if self._screenshot_target != "hud" else "HUD"
        if self._screenshot_paste:
            self._status_label.setText(
                f"copied {target_name} ({size}) · pasting …"
            )
            QTimer.singleShot(self._PASTE_DELAY_MS, self._paste_screenshot)
        else:
            self._status_label.setText(
                f"copied {target_name} ({size}) · press Ctrl+V to paste"
            )
        self._screenshot_status_timer.start(self._STATUS_RESET_MS)

    def _paste_screenshot(self) -> None:
        if paste_shortcut():
            self._status_label.setText("screenshot pasted")
        else:
            self._status_label.setText(
                "copied screenshot · press Ctrl+V to paste"
            )

    def _restore_status(self) -> None:
        self._status_label.setText(self._page_status_text())

    def _page_status_text(self) -> str:
        if self._page_loaded:
            return f"loaded {self._current_url()}"
        return f"loading {self._current_url()} …"

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
        self._apply_opacity()

        self._sync_window_flags()
        if apply_native:
            messages: tuple[str, ...] = ()
            try:
                ok, messages = set_input_transparency(
                    int(self.winId()), transparent
                )
            except Exception as exc:  # noqa: BLE001 — best-effort toggle
                ok = False
                _log(f"failed to change input mode: {exc}")
            if not ok:
                for message in messages:
                    _log(message)

        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent
        )
        self._web_view.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent
        )
        if hasattr(self, "_close_btn"):
            self._close_btn.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent
            )
            self._close_btn.setVisible(not transparent)
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

    def set_base_opacity(self, opacity: float) -> None:
        self._base_opacity = max(0.1, min(1.0, float(opacity)))
        self._apply_opacity()

    def _apply_opacity(self) -> None:
        target = (
            self._base_opacity
            if self._interactive
            else self._click_through_opacity
        )
        self.setWindowOpacity(max(0.1, min(1.0, target)))

    def _update_mode_ui(self) -> None:
        hint = hotkey_label(self._hotkey.spec)
        if self._hotkey.register_error:
            hint = f"{hint} unavailable"
        hide_hint = hotkey_label(self._hide_hotkey.spec)
        if self._hide_hotkey.register_error:
            hide_hint = f"{hide_hint} unavailable"
        snap_hint = hotkey_label(self._screenshot_hotkey.spec)
        if self._screenshot_hotkey.register_error:
            snap_hint = f"{snap_hint} unavailable"
        quit_hint = hotkey_label(self._quit_hotkey.spec)
        if self._quit_hotkey.register_error:
            quit_hint = f"{quit_hint} unavailable"
        if self._interactive:
            self._mode_label.setText(
                f"interactive · {hint} lock · {hide_hint} clear · "
                f"{snap_hint} snap · {quit_hint} quit"
            )
            mode_color = "rgba(120, 190, 255, 255)"
            border = "rgba(90, 170, 255, 200)"
        else:
            self._mode_label.setText(
                f"click-through · {hint} interact · {hide_hint} clear · "
                f"{snap_hint} snap · {quit_hint} quit"
            )
            mode_color = "rgba(150, 160, 175, 230)"
            border = "rgba(255, 255, 255, 20)"
        self._mode_label.setStyleSheet(f"color: {mode_color};")
        self.setStyleSheet(
            "#hudPanel { background-color: rgba(18, 20, 26, 190); "
            f"border-radius: 10px; border: 1px solid {border}; }}"
        )

    # -- slots (run on the GUI thread) -------------------------------------

    def _on_load_started(self) -> None:
        self._screenshot_status_timer.stop()
        self._page_loaded = False
        self._status_label.setText(self._page_status_text())

    def _on_load_progress(self, progress: int) -> None:
        self._status_label.setText(
            f"loading {self._current_url()} … {progress}%"
        )

    def _on_load_finished(self, ok: bool) -> None:
        self._screenshot_status_timer.stop()
        self._page_loaded = bool(ok)
        if ok:
            self._status_label.setText(self._page_status_text())
        else:
            self._status_label.setText(f"failed to load {self._page_url}")

    def _current_url(self) -> str:
        url = self._web_view.url().toString()
        return url or self._page_url

    def _quit_app(self) -> None:
        """Cleanly close and exit the application."""
        app = QApplication.instance()
        if app is not None:
            app.quit()
        else:
            self.close()

    def shutdown(self) -> None:
        self._detach_hotkeys()


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
                        help="Window opacity in interactive mode, 0.0–1.0 "
                             "(default: 1.0)")
    parser.add_argument("--click-through-opacity", type=float, default=0.35,
                        help="Window opacity in click-through mode, 0.1–1.0 "
                             "(default: 0.35)")
    parser.add_argument("--move-step", type=int, default=40,
                        help="Pixels per Ctrl+Alt+Arrow nudge (default: 40)")
    parser.add_argument("--hotkey", default="ctrl+alt+x",
                        help="Global hotkey that toggles click-through "
                             "(default: ctrl+alt+x)")
    parser.add_argument("--hide-hotkey", default="ctrl+alt+c",
                        help="Global hotkey that clears/hides the overlay and "
                             "brings it back (default: ctrl+alt+c)")
    parser.add_argument("--screenshot-hotkey", default="ctrl+alt+s",
                        help="Global hotkey that copies a screenshot of the "
                             "screen to the clipboard (default: ctrl+alt+s)")
    parser.add_argument("--quit-hotkey", default="ctrl+alt+q",
                        help="Global hotkey that exits the HUD application "
                             "(default: ctrl+alt+q)")
    parser.add_argument("--screenshot-target", choices=("screen", "hud"),
                        default="screen",
                        help="Target for screenshots: 'screen' captures the "
                             "main display screen, 'hud' captures the "
                             "embedded HUD page (default: screen)")
    parser.add_argument("--screenshot-paste", choices=("auto", "clipboard"),
                        default="auto",
                        help="'auto' also pastes the screenshot into the "
                             "focused window with Ctrl+V; 'clipboard' only "
                             "copies it (default: auto)")
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

    if not 0.1 <= args.click_through_opacity <= 1.0:
        parser.error("--click-through-opacity must be between 0.1 and 1.0")
    if args.move_step < 1:
        parser.error("--move-step must be at least 1")

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
            hide_hotkey=args.hide_hotkey,
            screenshot_hotkey=args.screenshot_hotkey,
            quit_hotkey=args.quit_hotkey,
            screenshot_paste=args.screenshot_paste == "auto",
            screenshot_target=args.screenshot_target,
            start_click_through=args.start_click_through,
            click_through_opacity=args.click_through_opacity,
            move_step=args.move_step,
        )
    except ValueError as exc:
        parser.error(str(exc))
    hud.set_base_opacity(args.opacity)
    hud.show()
    hud.position_top_right()

    app.aboutToQuit.connect(hud.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
