#!/usr/bin/env python3
"""Global hotkey support for the HUD (Windows RegisterHotKey).

The hotkey is registered against the HUD's native window handle, so it works
regardless of which app currently has focus. WM_HOTKEY messages are observed
through Qt's native event filter and surfaced as the `activated` signal.

On non-Windows platforms `attach()` returns False with a reason; nothing is
registered.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import (
    QAbstractNativeEventFilter,
    QCoreApplication,
    QObject,
    pyqtSignal,
)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
    "super": MOD_WIN,
}

_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
}

_DISPLAY_MODIFIERS = {
    MOD_CONTROL: "Ctrl",
    MOD_ALT: "Alt",
    MOD_SHIFT: "Shift",
    MOD_WIN: "Win",
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse 'ctrl+alt+h' into (MOD_* bitmask, virtual-key code)."""
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if not parts:
        raise ValueError("hotkey is empty")

    modifiers = 0
    vk: int | None = None
    for part in parts:
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
            continue
        if vk is not None:
            raise ValueError(f"hotkey has more than one key: {spec!r}")
        if part in _KEYS:
            vk = _KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            number = int(part[1:])
            if not 1 <= number <= 24:
                raise ValueError(f"function key out of range: {part!r}")
            vk = 0x70 + number - 1
        else:
            raise ValueError(f"unknown hotkey key: {part!r}")

    if vk is None:
        raise ValueError(f"hotkey has no key, only modifiers: {spec!r}")
    return modifiers, vk


def hotkey_label(spec: str) -> str:
    """Return a display form such as 'Ctrl+Alt+H'."""
    parts = [part.strip() for part in spec.split("+") if part.strip()]
    labels: list[str] = []
    for part in parts:
        lower = part.lower()
        if lower in _MODIFIERS:
            labels.append(_DISPLAY_MODIFIERS[_MODIFIERS[lower]])
        elif len(part) == 1:
            labels.append(part.upper())
        elif lower.startswith("f") and lower[1:].isdigit():
            labels.append(lower.upper())
        else:
            labels.append(part.capitalize())
    return "+".join(labels)


class _NativeFilter(QAbstractNativeEventFilter):
    """Routes WM_HOTKEY messages to its owning GlobalHotkey."""

    def __init__(self, hotkey: "GlobalHotkey") -> None:
        super().__init__()
        self._hotkey = hotkey

    def nativeEventFilter(self, event_type, message):
        return self._hotkey._handle_native_event(event_type, message)


class GlobalHotkey(QObject):
    """A system-wide hotkey bound to the HUD's native window handle."""

    activated = pyqtSignal()

    def __init__(self, spec: str = "ctrl+alt+h", parent: QObject | None = None):
        super().__init__(parent)
        self.spec = spec
        self._modifiers, self._vk = parse_hotkey(spec)
        self._id = 0x4A31
        self._hwnd: int | None = None
        self._registered = False
        self.register_error: str | None = None
        self._win32 = sys.platform == "win32"

        self._filter = _NativeFilter(self)
        app = QCoreApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._filter)

        if self._win32:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.RegisterHotKey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                wintypes.UINT,
                wintypes.UINT,
            ]
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.UnregisterHotKey.restype = wintypes.BOOL
            self._user32 = user32

    @property
    def registered(self) -> bool:
        return self._registered

    def attach(self, hwnd: int) -> bool:
        """Register the hotkey against *hwnd* (re-registering if it changed)."""
        if not self._win32:
            self.register_error = "global hotkeys are Windows-only"
            return False
        if self._registered and self._hwnd == hwnd:
            return True

        self.detach()
        self._hwnd = hwnd
        ctypes.set_last_error(0)
        if not self._user32.RegisterHotKey(
            hwnd, self._id, self._modifiers | MOD_NOREPEAT, self._vk
        ):
            error = ctypes.get_last_error()
            detail = ctypes.FormatError(error).strip() if error else "unknown error"
            self.register_error = f"{detail} (error {error})"
            return False

        self._registered = True
        self.register_error = None
        return True

    def detach(self) -> None:
        """Unregister the hotkey, if any."""
        if self._registered and self._hwnd is not None:
            self._user32.UnregisterHotKey(self._hwnd, self._id)
        self._registered = False
        self._hwnd = None

    def _handle_native_event(self, event_type, message):
        if not self._registered:
            return False, 0
        if bytes(event_type) != b"windows_generic_MSG":
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY and int(msg.wParam) == self._id:
            self.activated.emit()
            return True, 0
        return False, 0
