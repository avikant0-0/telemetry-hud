#!/usr/bin/env python3
"""Screenshot capture, area selection, and clipboard paste helpers for the HUD.

The primary/main screen is captured using Qt's screen grab facilities. Because
the HUD window is excluded from screen capture on Windows via
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`, the HUD overlay itself is
omitted from the screenshot while capturing the underlying desktop/apps.

A saved screen-relative capture region (`screenshot.json`) crops every capture
to the area chosen with the HUD's region picker; no region means full screen.
`RegionSelector` provides the full-screen drag-to-select overlay.

After the image is placed on the system clipboard, `paste_shortcut()` can
synthesize Ctrl+V with `SendInput`, dropping it into whichever window has
focus — for example a Google AI prompt box in your browser. Pasting into the
embedded HUD page itself is done by ``hud.py`` with a QWebEngine paste action,
because the HUD never owns the foreground window.

On non-Windows platforms capture and clipboard still work; auto-paste is a
no-op and returns False.
"""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QScreen,
)
from PyQt6.QtWidgets import QApplication, QWidget

if getattr(sys, "frozen", False):
    _REGION_CONFIG_PATH = Path(sys.executable).resolve().parent / "screenshot.json"
else:
    _REGION_CONFIG_PATH = Path(__file__).resolve().parent / "screenshot.json"

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


def capture_screen(
    screen: QScreen | None = None, region: QRect | None = None
) -> QPixmap | None:
    """Return pixels of the main screen (or *region* of it), or None."""
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        screens = QGuiApplication.screens()
        if screens:
            screen = screens[0]
    if screen is None:
        return None
    pixmap = screen.grabWindow(0)
    if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
        return None
    if region is not None:
        pixmap = _crop_region(pixmap, region, screen)
    return pixmap


def _crop_region(pixmap: QPixmap, region: QRect, screen: QScreen) -> QPixmap:
    screen_size = screen.geometry().size()
    if (
        region.width() <= 0
        or region.height() <= 0
        or region.x() >= screen_size.width()
        or region.y() >= screen_size.height()
    ):
        return pixmap
    x = max(0, region.x())
    y = max(0, region.y())
    width = min(region.width(), screen_size.width() - x)
    height = min(region.height(), screen_size.height() - y)
    dpr = pixmap.devicePixelRatio() or 1.0
    device_rect = QRect(
        int(round(x * dpr)),
        int(round(y * dpr)),
        int(round(width * dpr)),
        int(round(height * dpr)),
    ).intersected(QRect(0, 0, pixmap.width(), pixmap.height()))
    if device_rect.isEmpty():
        return pixmap
    return pixmap.copy(device_rect)


def load_capture_region() -> QRect | None:
    """Load the saved screenshot area; None means the full screen."""
    try:
        with open(_REGION_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        x = int(data["x"])
        y = int(data["y"])
        width = int(data["width"])
        height = int(data["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return QRect(x, y, width, height)


def save_capture_region(region: QRect | None) -> None:
    """Persist the screenshot area; None restores the full screen."""
    if region is None:
        data = None
    else:
        data = {
            "x": region.x(),
            "y": region.y(),
            "width": region.width(),
            "height": region.height(),
        }
    try:
        with open(_REGION_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


class RegionSelector(QWidget):
    """Full-screen overlay for drag-selecting the screenshot area."""

    region_selected = pyqtSignal(QRect)
    full_screen_selected = pyqtSignal()
    finished = pyqtSignal()

    def __init__(
        self, current: QRect | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        screen = QGuiApplication.primaryScreen()
        self._screen_rect = (
            screen.geometry() if screen is not None else QRect(0, 0, 1920, 1080)
        )
        self._current_region = current
        self._origin: QPoint | None = None
        self._cursor: QPoint | None = None
        self.setGeometry(self._screen_rect)

    def _selection_rect(self) -> QRect | None:
        if self._origin is None or self._cursor is None:
            return None
        return QRect(self._origin, self._cursor).normalized().intersected(self.rect())

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))

        if self._current_region is not None:
            painter.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.PenStyle.DashLine))
            painter.drawRect(self._current_region)

        selection = self._selection_rect()
        if selection is not None:
            painter.fillRect(selection, QColor(125, 211, 252, 35))
            painter.setPen(QPen(QColor("#7DD3FC"), 2))
            painter.drawRect(selection)
            painter.setPen(QColor("#E2E8F0"))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(
                selection.adjusted(6, 6, -6, -6),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                f"{selection.width()} × {selection.height()}",
            )

        painter.setPen(QColor(255, 255, 255, 230))
        painter.setFont(QFont("Segoe UI", 10))
        painter.drawText(
            QRect(0, 16, self.width(), 28),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Drag to select the screenshot area   ·   Esc to cancel   ·   R for full screen",
        )
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._cursor = self._origin
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._origin is not None:
            self._cursor = event.position().toPoint()
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            super().mouseReleaseEvent(event)
            return
        self._cursor = event.position().toPoint()
        selection = self._selection_rect()
        self._origin = None
        self._cursor = None
        if selection is not None and selection.width() >= 8 and selection.height() >= 8:
            self.region_selected.emit(selection)
            self.close()
        else:
            self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        if event.key() == Qt.Key.Key_R:
            self.full_screen_selected.emit()
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finished.emit()
        super().closeEvent(event)


def capture_widget(widget: QWidget) -> QPixmap | None:
    """Return *widget*'s rendered pixels, or None if it cannot be captured."""
    if not widget.isVisible() or widget.width() <= 0 or widget.height() <= 0:
        return None
    pixmap = widget.grab()
    if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
        return None
    return pixmap


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key_event(vk: int, flags: int = 0) -> _INPUT:
    return _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, dwFlags=flags)),
    )


def paste_shortcut() -> bool:
    """Send Ctrl+V to the focused window. Windows only; True on success."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(_INPUT),
        ctypes.c_int,
    ]
    user32.SendInput.restype = wintypes.UINT
    inputs = (
        _key_event(VK_MENU, KEYEVENTF_KEYUP),
        _key_event(VK_CONTROL),
        _key_event(VK_V),
        _key_event(VK_V, KEYEVENTF_KEYUP),
        _key_event(VK_CONTROL, KEYEVENTF_KEYUP),
    )
    sent = user32.SendInput(len(inputs), (_INPUT * len(inputs))(*inputs), ctypes.sizeof(_INPUT))
    return sent == len(inputs)


def main() -> None:
    import argparse
    import time

    parser = argparse.ArgumentParser(
        description="Capture the main screen to the system clipboard."
    )
    parser.add_argument(
        "--paste",
        action="store_true",
        help="Synthesize Ctrl+V after copying to clipboard",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Delay in seconds before capturing (default: 0.0)",
    )
    parser.add_argument(
        "--save",
        type=str,
        default="",
        help="Optional image file path to save (e.g. screenshot.png)",
    )
    args = parser.parse_args()

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    if args.delay > 0:
        time.sleep(args.delay)

    pixmap = capture_screen()
    if pixmap is None:
        if sys.stderr is not None:
            print("Failed to capture screen", file=sys.stderr)
        sys.exit(1)

    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setPixmap(pixmap)

    if args.save:
        pixmap.save(args.save)

    if args.paste:
        time.sleep(0.2)
        paste_shortcut()


if __name__ == "__main__":
    main()
