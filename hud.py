#!/usr/bin/env python3
"""
Telemetry HUD — a borderless, always-on-top overlay that embeds a web page
(https://www.google.com by default).

The overlay starts interactive: clicks and typing are delivered to the page
without ever activating the window, so the app underneath keeps focus and
never sees a window switch. A system-wide hotkey (Ctrl+Alt+X by default)
toggles click-through mode, in which mouse and keyboard input pass through to
whatever is underneath.
Ctrl+Alt+C clears (hides) the overlay completely; the same hotkey brings it back.
Ctrl+Alt+Arrow keys nudge the overlay in any mode, and click-through mode
dims the window so it stays unobtrusive. Ctrl+Alt+S grabs a screenshot of the
main screen, copies it to the clipboard, and (by default) pastes it into the
page you were typing in, or into the focused window when your last click was
outside the HUD — e.g. a Google AI prompt box.
Ctrl+Alt+Q closes and exits the application cleanly.

Design notes
------------
* The window is frameless and always-on-top, and it never appears in the
  taskbar or Alt+Tab (`Tool` hint plus `WS_EX_TOOLWINDOW`).
* The window is a non-activating "glass" overlay: `WS_EX_NOACTIVATE` is kept
  in both modes, so clicking it never moves the foreground window. While
  interactive, a low-level input gate (`input_capture.py`) captures typing and
  the mouse wheel and replays them into the embedded page; LWIN combos, Alt
  shortcuts, and the HUD's own hotkeys pass through. While click-through, the
  window also ignores mouse input (`Qt.WindowTransparentForInput` plus
  `WS_EX_TRANSPARENT`), so input goes to the app underneath.
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
  be pasted into the HUD page or, when the last click was outside, into the
  focused window with a synthesized Ctrl+V. Under capture exclusion, the HUD
  window itself is omitted from the captured screen image.

Usage
-----
    python hud.py --page https://www.google.com
"""

from __future__ import annotations

import argparse
import signal
import sys

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QFont,
    QGuiApplication,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
    QShowEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


def _log(message: str) -> None:
    """Best-effort diagnostics (windowed builds have no stderr)."""
    if sys.stderr is not None:
        print(f"[hud] {message}", file=sys.stderr)


try:
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover
    _log(
        "The 'PyQt6-WebEngine' package is required for the embedded page. "
        "Install with: pip install PyQt6-WebEngine"
    )
    raise

from hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, GlobalHotkey, hotkey_label
from hotkey_ui import (
    SettingsView,
    decompose_hotkey,
    load_hotkeys_config,
    save_hotkeys_config,
)
from input_capture import InputGate
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
    _SLIDE_HOTKEY_ID = 0x4A40        # Ctrl+Shift+Arrow hold-to-repeat IDs (4 directions: 0x4A40-0x4A43)
    _HIDE_HOTKEY_ID = 0x4A36
    _SCREENSHOT_HOTKEY_ID = 0x4A37
    _QUIT_HOTKEY_ID = 0x4A38
    _OPACITY_UP_HOTKEY_ID = 0x4A39
    _OPACITY_DOWN_HOTKEY_ID = 0x4A3A
    _PASTE_DELAY_MS = 250
    _STATUS_RESET_MS = 3000
    _SLIDE_REPEAT_INITIAL_MS = 50   # first repeat delay (ms)
    _SLIDE_REPEAT_INTERVAL_MS = 30  # subsequent repeat interval (ms)
    _OPACITY_STEP = 0.1             # opacity change per hotkey press

    # VK codes for the four arrow keys (used by GetAsyncKeyState polling)
    _VK_LEFT = 0x25
    _VK_UP = 0x26
    _VK_RIGHT = 0x27
    _VK_DOWN = 0x28
    _VK_CONTROL = 0x11
    _VK_SHIFT = 0x10
    _VK_ALT = 0x12
    _VK_LWIN = 0x5B

    # Map normalized modifier names (from decompose_hotkey) to VK codes
    _MODIFIER_VK = {
        "ctrl": _VK_CONTROL,
        "alt": _VK_ALT,
        "shift": _VK_SHIFT,
        "win": _VK_LWIN,
    }

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
        opacity_up_hotkey: str = "ctrl+alt+m",
        opacity_down_hotkey: str = "ctrl+alt+n",
        screenshot_paste: bool = True,
        screenshot_target: str = "screen",
        allow_microphone: bool = True,
        start_click_through: bool = False,
        click_through_opacity: float = 0.35,
        move_step: int = 40,
        move_modifiers: str = "ctrl+alt",
    ) -> None:
        super().__init__()
        self._page_url = page_url
        self._fixed_width = width
        self._fixed_height = height
        self._capture_exclusion = capture_exclusion
        self._screenshot_target = screenshot_target
        self._allow_microphone = bool(allow_microphone)
        self._interactive = not start_click_through
        self._base_opacity = 1.0
        self._click_through_opacity = max(
            0.1, min(1.0, float(click_through_opacity))
        )
        self._move_step = max(1, int(move_step))
        self._move_modifiers = move_modifiers or "ctrl+alt"
        self._stealth_hwnd: int | None = None
        self.stealth_report: StealthReport | None = None
        self._drag_origin: QPoint | None = None
        self._drag_window_origin: QPoint | None = None
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
        self._opacity_up_hotkey = GlobalHotkey(
            opacity_up_hotkey, self, hotkey_id=self._OPACITY_UP_HOTKEY_ID
        )
        self._opacity_up_hotkey.activated.connect(self._increase_opacity)
        self._opacity_down_hotkey = GlobalHotkey(
            opacity_down_hotkey, self, hotkey_id=self._OPACITY_DOWN_HOTKEY_ID
        )
        self._opacity_down_hotkey.activated.connect(self._decrease_opacity)
        self._screenshot_status_timer = QTimer(self)
        self._screenshot_status_timer.setSingleShot(True)
        self._screenshot_status_timer.timeout.connect(self._restore_status)
        self._move_hotkeys = self._build_move_hotkeys()
        self._slide_hotkeys = self._build_slide_hotkeys()
        # Repeat-slide state: timer fires continuously while Ctrl+Shift+Arrow held
        self._slide_timer = QTimer(self)
        self._slide_timer.timeout.connect(self._on_slide_tick)
        self._slide_dx: int = 0
        self._slide_dy: int = 0
        self._build_window()
        self._input_gate = InputGate(self)
        self._input_gate.set_passthrough_hotkeys(
            {(key.modifiers, key.vk) for key in self._all_hotkeys()}
        )
        self._input_gate.key_event.connect(
            self._inject_key, Qt.ConnectionType.QueuedConnection
        )
        self._input_gate.wheel_event.connect(
            self._inject_wheel, Qt.ConnectionType.QueuedConnection
        )
        self._build_ui()

    def _all_hotkeys(self) -> tuple[GlobalHotkey, ...]:
        return (
            self._hotkey,
            self._hide_hotkey,
            self._screenshot_hotkey,
            self._quit_hotkey,
            self._opacity_up_hotkey,
            self._opacity_down_hotkey,
            *self._move_hotkeys,
            *self._slide_hotkeys,
        )

    def _build_move_hotkeys(self) -> list[GlobalHotkey]:
        hotkeys: list[GlobalHotkey] = []
        directions = (("left", -1, 0), ("up", 0, -1), ("right", 1, 0), ("down", 0, 1))
        mod_prefix = self._move_modifiers or "ctrl+alt"
        for offset, (dir_name, dx, dy) in enumerate(directions):
            spec = f"{mod_prefix}+{dir_name}"
            hotkey = GlobalHotkey(
                spec, self, hotkey_id=self._MOVE_HOTKEY_ID + offset
            )
            hotkey.activated.connect(
                lambda _=False, dx=dx, dy=dy: self._start_slide(dx, dy)
            )
            hotkeys.append(hotkey)
        return hotkeys

    def _build_slide_hotkeys(self) -> list[GlobalHotkey]:
        """Build Ctrl+Shift+Arrow hotkeys that start a hold-to-repeat slide."""
        hotkeys: list[GlobalHotkey] = []
        directions = (("left", -1, 0), ("up", 0, -1), ("right", 1, 0), ("down", 0, 1))
        for offset, (dir_name, dx, dy) in enumerate(directions):
            spec = f"ctrl+shift+{dir_name}"
            hk = GlobalHotkey(spec, self, hotkey_id=self._SLIDE_HOTKEY_ID + offset)
            hk.activated.connect(
                lambda _=False, dx=dx, dy=dy: self._start_slide(dx, dy)
            )
            hotkeys.append(hk)
        return hotkeys

    def _start_slide(self, dx: int, dy: int) -> None:
        """Begin continuous sliding in direction (dx, dy) while key is held."""
        self._slide_dx = dx
        self._slide_dy = dy
        # Immediately move once
        self.move_by(dx * self._move_step, dy * self._move_step)
        # Start repeating timer (fires repeatedly every interval)
        self._slide_timer.setInterval(self._SLIDE_REPEAT_INTERVAL_MS)
        if not self._slide_timer.isActive():
            self._slide_timer.start()

    def _on_slide_tick(self) -> None:
        """Called every interval while slide timer runs; stops when key released."""
        # Map direction to VK code
        vk_map = {
            (-1, 0): self._VK_LEFT,
            (0, -1): self._VK_UP,
            (1, 0): self._VK_RIGHT,
            (0, 1): self._VK_DOWN,
        }
        arrow_vk = vk_map.get((self._slide_dx, self._slide_dy))
        try:
            import ctypes as _ctypes
            user32 = _ctypes.windll.user32
            mods, _ = decompose_hotkey(self._move_modifiers or "ctrl+alt")
            held = all(
                bool(user32.GetAsyncKeyState(self._MODIFIER_VK[mod]) & 0x8000)
                for mod in mods
                if mod in self._MODIFIER_VK
            )
            arrow_down = arrow_vk is not None and bool(
                user32.GetAsyncKeyState(arrow_vk) & 0x8000
            )
        except Exception:
            self._slide_timer.stop()
            return
        if held and arrow_down:
            self.move_by(self._slide_dx * self._move_step, self._slide_dy * self._move_step)
        else:
            self._slide_timer.stop()

    def _update_move_hotkeys(self, new_modifiers_spec: str) -> bool:
        mods, _ = decompose_hotkey(new_modifiers_spec)
        ordered = [m for m in ("ctrl", "alt", "shift", "win") if m in mods]
        mod_prefix = "+".join(ordered) if ordered else "ctrl+alt"
        self._move_modifiers = mod_prefix

        for hk in self._move_hotkeys:
            hk.detach()

        directions = (("left", -1, 0), ("up", 0, -1), ("right", 1, 0), ("down", 0, 1))
        all_ok = True
        hwnd = int(self.winId()) if self.isVisible() else None
        new_hotkeys = []
        for offset, (dir_name, dx, dy) in enumerate(directions):
            spec = f"{mod_prefix}+{dir_name}"
            hk = GlobalHotkey(spec, self, hotkey_id=self._MOVE_HOTKEY_ID + offset)
            hk.activated.connect(
                lambda _=False, dx=dx, dy=dy: self._start_slide(dx, dy)
            )
            if hwnd is not None:
                if not hk.attach(hwnd):
                    all_ok = False
            new_hotkeys.append(hk)
        self._move_hotkeys = new_hotkeys
        return all_ok

    def _save_current_hotkeys(self) -> None:
        cfg = {
            "hide": self._hide_hotkey.spec,
            "click_through": self._hotkey.spec,
            "screenshot": self._screenshot_hotkey.spec,
            "quit": self._quit_hotkey.spec,
            "opacity_up": self._opacity_up_hotkey.spec,
            "opacity_down": self._opacity_down_hotkey.spec,
            "move": self._move_modifiers,
        }
        save_hotkeys_config(cfg)

    def _on_hotkey_changed(self, action_id: str, new_spec: str) -> None:
        _log(f"hotkey '{action_id}' changed to '{new_spec}'")
        ok = True
        error_msg = None

        if action_id == "hide":
            ok = self._hide_hotkey.rebind(new_spec)
            error_msg = self._hide_hotkey.register_error
        elif action_id == "click_through":
            ok = self._hotkey.rebind(new_spec)
            error_msg = self._hotkey.register_error
        elif action_id == "screenshot":
            ok = self._screenshot_hotkey.rebind(new_spec)
            error_msg = self._screenshot_hotkey.register_error
            if ok:
                self._snap_btn.setToolTip(
                    f"Screenshot ({hotkey_label(self._screenshot_hotkey.spec)})"
                )
        elif action_id == "quit":
            ok = self._quit_hotkey.rebind(new_spec)
            error_msg = self._quit_hotkey.register_error
        elif action_id == "opacity_up":
            ok = self._opacity_up_hotkey.rebind(new_spec)
            error_msg = self._opacity_up_hotkey.register_error
        elif action_id == "opacity_down":
            ok = self._opacity_down_hotkey.rebind(new_spec)
            error_msg = self._opacity_down_hotkey.register_error
        elif action_id == "move":
            ok = self._update_move_hotkeys(new_spec)
            if not ok:
                error_msg = "failed to bind move hotkeys"

        if not ok:
            self._settings_view.set_card_error(
                action_id, error_msg or "Failed to register hotkey with system"
            )
        else:
            self._settings_view.set_card_error(action_id, None)
            self._input_gate.set_passthrough_hotkeys(
                {(key.modifiers, key.vk) for key in self._all_hotkeys()}
            )
            self._save_current_hotkeys()

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
        self._sync_input_gate()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._input_gate.disable()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WinIdChange:
            self._stealth_hwnd = None
            self._detach_hotkeys()
            if self.isVisible():
                QTimer.singleShot(0, self._apply_stealth)
                QTimer.singleShot(0, self._register_hotkey)
                QTimer.singleShot(0, self._sync_input_gate)
        return super().event(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.ActivationChange
            and self._interactive
            and self.isActiveWindow()
        ):
            self._focus_page()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Clicks on the frame/header/status area must still put the keyboard
        # focus into the embedded page. The window deliberately never becomes
        # active; the global input gate handles typing without a focus change.
        # A press on the frame (anything outside the page) also starts a drag.
        if self._interactive and event.button() == Qt.MouseButton.LeftButton:
            if not (hasattr(self, "_stack") and hasattr(self, "_settings_view") and self._stack.currentWidget() == self._settings_view):
                self._focus_page()
            self._drag_origin = event.globalPosition().toPoint()
            self._drag_window_origin = self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._drag_origin is not None
            and self._drag_window_origin is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            delta = event.globalPosition().toPoint() - self._drag_origin
            target = self._drag_window_origin + delta
            bounds = self._virtual_bounds()
            if bounds is not None:
                target.setX(
                    max(bounds.left(),
                        min(target.x(), bounds.right() - self.width() + 1))
                )
                target.setY(
                    max(bounds.top(),
                        min(target.y(), bounds.bottom() - self.height() + 1))
                )
            self.move(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._drag_origin is not None
        ):
            self._drag_origin = None
            self._drag_window_origin = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

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

        self._snap_btn = QPushButton("◉")
        self._snap_btn.setToolTip(
            f"Screenshot ({hotkey_label(self._screenshot_hotkey.spec)})"
        )
        self._snap_btn.setFixedSize(18, 18)
        self._snap_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._snap_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,160); color: #E2E8F0; "
            "border: none; font-size: 12px; font-weight: bold; border-radius: 3px; } "
            "QPushButton:hover { color: #ffffff; background: rgba(0, 0, 0, 220); }"
        )
        self._snap_btn.clicked.connect(self.copy_screenshot)

        self._back_btn = QPushButton("‹")
        self._back_btn.setToolTip("Back")
        self._back_btn.setFixedSize(18, 18)
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,160); color: #E2E8F0; "
            "border: none; font-size: 14px; font-weight: bold; border-radius: 3px; } "
            "QPushButton:hover { color: #ffffff; background: rgba(0, 0, 0, 220); } "
            "QPushButton:disabled { color: rgba(200, 200, 200, 80); }"
        )
        self._back_btn.setEnabled(False)
        self._back_btn.clicked.connect(self._go_back)

        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setToolTip("Settings")
        self._settings_btn.setFixedSize(18, 18)
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,160); color: #E2E8F0; "
            "border: none; font-size: 13px; font-weight: bold; border-radius: 3px; } "
            "QPushButton:hover { color: #ffffff; background: rgba(0, 0, 0, 220); }"
        )
        self._settings_btn.clicked.connect(self.toggle_settings)

        self._close_btn = QPushButton("✕")
        self._close_btn.setToolTip("Close HUD")
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,160); color: #E2E8F0; "
            "border: none; font-size: 11px; font-weight: bold; border-radius: 3px; } "
            "QPushButton:hover { color: #ff6b6b; background: rgba(0, 0, 0, 220); }"
        )
        self._close_btn.clicked.connect(self._quit_app)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)
        header_layout.addStretch(1)
        header_layout.addWidget(self._snap_btn, 0)
        header_layout.addWidget(self._back_btn, 0)
        header_layout.addWidget(self._settings_btn, 0)
        header_layout.addWidget(self._close_btn, 0)

        # Wrap the icon row in a black pill container so icons are always visible
        self._header_bar = QWidget()
        self._header_bar.setObjectName("headerBar")
        self._header_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._header_bar.setStyleSheet(
            "#headerBar { background-color: rgba(0, 0, 0, 200); border-radius: 6px; }"
        )
        self._header_bar.setLayout(header_layout)

        self._status_label = QLabel(f"loading {self._page_url} …")
        self._status_label.setFont(QFont("Menlo", 8))
        self._status_label.setStyleSheet("color: #A0AEC0; background: transparent;")
        self._status_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )

        self._web_view = QWebEngineView(self)
        self._web_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._web_view.page().featurePermissionRequested.connect(
            self._on_feature_permission_requested
        )
        self._web_view.loadStarted.connect(self._on_load_started)
        self._web_view.loadProgress.connect(self._on_load_progress)
        self._web_view.loadFinished.connect(self._on_load_finished)
        self._web_view.urlChanged.connect(self._update_back_button)

        self._stack = QStackedWidget(self)

        self._browser_container = QWidget()
        browser_layout = QVBoxLayout(self._browser_container)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        browser_layout.setSpacing(4)
        browser_layout.addWidget(self._status_label)
        browser_layout.addWidget(self._web_view, 1)

        self._settings_view = SettingsView(
            hotkeys={
                "hide": self._hide_hotkey.spec,
                "click_through": self._hotkey.spec,
                "screenshot": self._screenshot_hotkey.spec,
                "quit": self._quit_hotkey.spec,
                "opacity_up": self._opacity_up_hotkey.spec,
                "opacity_down": self._opacity_down_hotkey.spec,
                "move": self._move_modifiers,
            },
            parent=self,
        )
        self._settings_view.hotkey_changed.connect(self._on_hotkey_changed)
        self._settings_view.back_requested.connect(self.show_browser)

        self._stack.addWidget(self._browser_container)
        self._stack.addWidget(self._settings_view)

        layout.addWidget(self._header_bar)
        layout.addWidget(self._stack, 1)

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

    def show_settings(self) -> None:
        """Switch view to the settings panel."""
        self._stack.setCurrentWidget(self._settings_view)
        self._settings_btn.setStyleSheet(
            "QPushButton { background: rgba(255, 255, 255, 40); color: #E2E8F0; "
            "border: none; font-size: 13px; font-weight: bold; border-radius: 3px; }"
        )
        self._snap_btn.setVisible(False)
        self._back_btn.setVisible(False)

    def show_browser(self) -> None:
        """Switch view back to the browser overlay."""
        self._settings_view.cancel_recording()
        self._stack.setCurrentWidget(self._browser_container)
        self._settings_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,160); color: #E2E8F0; "
            "border: none; font-size: 13px; font-weight: bold; border-radius: 3px; } "
            "QPushButton:hover { color: #ffffff; background: rgba(0, 0, 0, 220); }"
        )
        self._snap_btn.setVisible(self._interactive)
        self._back_btn.setVisible(self._interactive)
        self._focus_page()

    def toggle_settings(self) -> None:
        """Toggle between browser and settings view."""
        if self._stack.currentWidget() == self._settings_view:
            self.show_browser()
        else:
            self.show_settings()

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
        if self._interactive and self._input_gate.engaged:
            # The HUD never owns the foreground window, so a synthesized
            # Ctrl+V would land in the app underneath. Paste into the page
            # the user was typing in instead.
            self._focus_page()
            self._web_view.page().triggerAction(
                QWebEnginePage.WebAction.Paste
            )
            self._status_label.setText("screenshot pasted into HUD")
            return
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
        # Never accept activation: the foreground window must stay put.
        window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)

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
        if hasattr(self, "_back_btn"):
            self._back_btn.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent
            )
            self._back_btn.setVisible(not transparent)
        if hasattr(self, "_snap_btn"):
            self._snap_btn.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent
            )
            self._snap_btn.setVisible(not transparent)
        if hasattr(self, "_settings_btn"):
            self._settings_btn.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, transparent
            )
            self._settings_btn.setVisible(not transparent)
        policy = (
            Qt.FocusPolicy.NoFocus if transparent
            else Qt.FocusPolicy.StrongFocus
        )
        self.setFocusPolicy(policy)
        self._web_view.setFocusPolicy(policy)
        if transparent:
            self._web_view.clearFocus()
            self.clearFocus()
        self._sync_input_gate()
        self._update_mode_ui()

    def _sync_input_gate(self) -> None:
        """Install or remove the global input hooks for the current mode."""
        if not self.isVisible():
            self._input_gate.disable()
            return
        self._input_gate.set_hwnd(int(self.winId()))
        if self._interactive:
            self._input_gate.enable()
        else:
            self._input_gate.disable()

    def set_base_opacity(self, opacity: float) -> None:
        self._base_opacity = max(0.1, min(1.0, float(opacity)))
        self._apply_opacity()

    def _increase_opacity(self) -> None:
        """Raise the HUD's base opacity by one step (clamped to 1.0)."""
        self.set_base_opacity(self._base_opacity + self._OPACITY_STEP)
        self._status_label.setText(f"opacity {self._base_opacity:.1f}")

    def _decrease_opacity(self) -> None:
        """Lower the HUD's base opacity by one step (clamped to 0.1)."""
        self.set_base_opacity(self._base_opacity - self._OPACITY_STEP)
        self._status_label.setText(f"opacity {self._base_opacity:.1f}")

    def _apply_opacity(self) -> None:
        target = (
            self._base_opacity
            if self._interactive
            else self._click_through_opacity
        )
        self.setWindowOpacity(max(0.1, min(1.0, target)))

    def _focus_page(self) -> None:
        """Give keyboard focus to the embedded page, if interactive."""
        if not self._interactive or not hasattr(self, "_web_view"):
            return
        self._web_view.setFocus(Qt.FocusReason.MouseFocusReason)

    def _page_target(self) -> QWidget:
        return self._web_view.focusProxy() or self._web_view

    def _inject_key(
        self,
        qt_key: int,
        is_release: bool,
        text: str,
        modifiers: int,
        autorep: bool,
    ) -> None:
        """Replay a captured global key stroke into the embedded page or hotkey recorder."""
        if hasattr(self, "_settings_view"):
            rec = self._settings_view.active_recorder
            if rec is not None:
                if not is_release:
                    rec.handle_key_stroke(qt_key, text)
                return

        if (
            hasattr(self, "_stack")
            and hasattr(self, "_settings_view")
            and self._stack.currentWidget() == self._settings_view
        ):
            return

        event_type = (
            QEvent.Type.KeyRelease if is_release else QEvent.Type.KeyPress
        )
        event = QKeyEvent(
            event_type,
            int(qt_key),
            Qt.KeyboardModifier(int(modifiers)),
            text or "",
            bool(autorep),
        )
        QApplication.sendEvent(self._page_target(), event)

    def _inject_wheel(
        self, dx: int, dy: int, global_x: int, global_y: int, modifiers: int
    ) -> None:
        """Replay a swallowed wheel event, using Qt logical coordinates."""
        target = self._page_target()
        ratio = float(self.devicePixelRatioF()) or 1.0
        global_pos = QPointF(global_x / ratio, global_y / ratio)
        local = target.mapFromGlobal(
            QPoint(round(global_pos.x()), round(global_pos.y()))
        )
        event = QWheelEvent(
            QPointF(local),
            global_pos,
            QPoint(0, 0),
            QPoint(int(dx), int(dy)),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier(int(modifiers)),
            Qt.ScrollPhase.NoScrollPhase,
            False,
            Qt.MouseEventSource.MouseEventNotSynthesized,
        )
        QApplication.sendEvent(target, event)

    def _update_mode_ui(self) -> None:
        border = (
            "rgba(90, 170, 255, 200)"
            if self._interactive
            else "rgba(255, 255, 255, 20)"
        )
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
        self._update_back_button()
        if ok:
            self._status_label.setText(self._page_status_text())
        else:
            self._status_label.setText(f"failed to load {self._page_url}")

    def _on_feature_permission_requested(
        self, origin: QUrl, feature: QWebEnginePage.Feature
    ) -> None:
        page = self._web_view.page()
        if (
            self._allow_microphone
            and feature == QWebEnginePage.Feature.MediaAudioCapture
        ):
            page.setFeaturePermission(
                origin,
                feature,
                QWebEnginePage.PermissionPolicy.PermissionGrantedByUser,
            )
            _log(f"microphone access granted to {origin.toString()}")
        else:
            page.setFeaturePermission(
                origin,
                feature,
                QWebEnginePage.PermissionPolicy.PermissionDeniedByUser,
            )

    def _go_back(self) -> None:
        if self._web_view.history().canGoBack():
            self._web_view.back()

    def _update_back_button(self, *_args) -> None:
        self._back_btn.setEnabled(self._web_view.history().canGoBack())

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
        self._input_gate.close()
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
    parser.add_argument("--opacity-up-hotkey", default="ctrl+alt+m",
                         help="Global hotkey that raises HUD opacity "
                              "(default: ctrl+alt+m)")
    parser.add_argument("--opacity-down-hotkey", default="ctrl+alt+n",
                         help="Global hotkey that lowers HUD opacity "
                              "(default: ctrl+alt+n)")
    parser.add_argument("--screenshot-target", choices=("screen", "hud"),
                        default="screen",
                        help="Target for screenshots: 'screen' captures the "
                             "main display screen, 'hud' captures the "
                             "embedded HUD page (default: screen)")
    parser.add_argument("--screenshot-paste", choices=("auto", "clipboard"),
                        default="auto",
                        help="'auto' also pastes the screenshot into the HUD "
                             "page or the focused window; 'clipboard' only "
                             "copies it (default: auto)")
    parser.add_argument("--microphone", choices=("allow", "deny"),
                        default="allow",
                        help="Microphone access for the embedded page: 'allow' "
                             "grants getUserMedia audio requests, 'deny' "
                             "blocks them (default: allow)")
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

    saved_cfg = load_hotkeys_config()
    passed = sys.argv[1:]

    def is_passed(flag: str) -> bool:
        return any(a == flag or a.startswith(f"{flag}=") for a in passed)

    hotkey_val = (
        args.hotkey if is_passed("--hotkey")
        else saved_cfg.get("click_through", args.hotkey)
    )
    hide_hotkey_val = (
        args.hide_hotkey if is_passed("--hide-hotkey")
        else saved_cfg.get("hide", args.hide_hotkey)
    )
    screenshot_hotkey_val = (
        args.screenshot_hotkey if is_passed("--screenshot-hotkey")
        else saved_cfg.get("screenshot", args.screenshot_hotkey)
    )
    quit_hotkey_val = (
        args.quit_hotkey if is_passed("--quit-hotkey")
        else saved_cfg.get("quit", args.quit_hotkey)
    )
    opacity_up_hotkey_val = (
        args.opacity_up_hotkey if is_passed("--opacity-up-hotkey")
        else saved_cfg.get("opacity_up", args.opacity_up_hotkey)
    )
    opacity_down_hotkey_val = (
        args.opacity_down_hotkey if is_passed("--opacity-down-hotkey")
        else saved_cfg.get("opacity_down", args.opacity_down_hotkey)
    )
    move_modifiers_val = saved_cfg.get("move", "ctrl+alt")

    try:
        hud = OverlayHUD(
            args.page,
            width=args.width,
            height=args.height,
            capture_exclusion=args.capture_exclusion,
            hotkey=hotkey_val,
            hide_hotkey=hide_hotkey_val,
            screenshot_hotkey=screenshot_hotkey_val,
            quit_hotkey=quit_hotkey_val,
            opacity_up_hotkey=opacity_up_hotkey_val,
            opacity_down_hotkey=opacity_down_hotkey_val,
            move_modifiers=move_modifiers_val,
            screenshot_paste=args.screenshot_paste == "auto",
            screenshot_target=args.screenshot_target,
            allow_microphone=args.microphone == "allow",
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
