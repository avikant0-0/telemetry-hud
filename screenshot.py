#!/usr/bin/env python3
"""Screenshot capture and clipboard paste helpers for the telemetry HUD.

The primary/main screen is captured using Qt's screen grab facilities. Because
the HUD window is excluded from screen capture on Windows via
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`, the HUD overlay itself is
omitted from the screenshot while capturing the underlying desktop/apps.

After the image is placed on the system clipboard, `paste_shortcut()` can
synthesize Ctrl+V with `SendInput`, dropping it into whichever window has
focus — for example, a Google AI prompt box in your browser or inside the HUD.

On non-Windows platforms capture and clipboard still work; auto-paste is a
no-op and returns False.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtGui import QGuiApplication, QPixmap, QScreen
from PyQt6.QtWidgets import QApplication, QWidget

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


def capture_screen(screen: QScreen | None = None) -> QPixmap | None:
    """Return pixels of the primary/main screen (or specified screen), or None."""
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
    return pixmap


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
