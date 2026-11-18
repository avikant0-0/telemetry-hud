#!/usr/bin/env python3
"""Global input gate for the telemetry HUD (Windows low-level hooks).

The HUD is a ``WS_EX_NOACTIVATE`` overlay: clicking it never changes the
foreground window. That keeps the app underneath focused, but it also means
the HUD cannot receive keyboard input through normal window activation and
that the mouse wheel (which Windows routes to the focused window) would
otherwise scroll the app underneath.

This module bridges that gap while the HUD is interactive:

* ``WH_MOUSE_LL`` watches mouse events globally. A left click inside the HUD
  engages keyboard capture; a left click outside releases it. Wheel events
  over the HUD are swallowed and re-emitted so the page under the pointer
  scrolls instead of the background application.
* ``WH_KEYBOARD_LL`` swallows key strokes while capture is engaged and
  re-emits them as Qt signals. The HUD injects them into the embedded page,
  so typing works without the background window ever losing focus or seeing
  the characters. Modifier keys, Windows-key shortcuts, Alt shortcuts
  (Alt+Tab, Alt+F4, …), and the HUD's own global hotkeys pass through to the
  system. IME composition keys are currently swallowed (typing them into the
  HUD is not supported yet).

The hook callbacks run on the Qt GUI thread and only classify/store events;
they return immediately and the actual Qt work happens on queued signals.
That keeps the hooks well inside Windows' low-level hook timeout.

On non-Windows platforms the class is an inert no-op.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN

_IS_WINDOWS = sys.platform == "win32"

_HC_ACTION = 0
_WH_KEYBOARD_LL = 13
_WH_MOUSE_LL = 14

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_RBUTTONDOWN = 0x0204
_WM_MBUTTONDOWN = 0x0207
_WM_MOUSEWHEEL = 0x020A
_WM_XBUTTONDOWN = 0x020B
_WM_MOUSEHWHEEL = 0x020E

_KEY_MESSAGES = (_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP)
_BUTTON_MESSAGES = (
    _WM_LBUTTONDOWN,
    _WM_RBUTTONDOWN,
    _WM_MBUTTONDOWN,
    _WM_XBUTTONDOWN,
)

_LLKHF_INJECTED = 0x10
_LLKHF_UP = 0x80

_GA_ROOT = 2

_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_PAUSE = 0x13
_VK_CAPITAL = 0x14
_VK_ESCAPE = 0x1B
_VK_SNAPSHOT = 0x2C
_VK_LSHIFT = 0xA0
_VK_RSHIFT = 0xA1
_VK_LCONTROL = 0xA2
_VK_RCONTROL = 0xA3
_VK_LMENU = 0xA4
_VK_RMENU = 0xA5
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_VK_NUMLOCK = 0x90
_VK_SCROLL = 0x91
_VK_PROCESSKEY = 0xE5
_VK_PACKET = 0xE7

_MODIFIER_VKS = frozenset({
    _VK_SHIFT, _VK_LSHIFT, _VK_RSHIFT,
    _VK_CONTROL, _VK_LCONTROL, _VK_RCONTROL,
    _VK_MENU, _VK_LMENU, _VK_RMENU,
    _VK_LWIN, _VK_RWIN,
})

_ALWAYS_PASS_VKS = frozenset({
    _VK_CAPITAL, _VK_NUMLOCK, _VK_SCROLL, _VK_SNAPSHOT, _VK_PAUSE,
    0xAD, 0xAE, 0xAF,  # volume mute / down / up
    0xB0, 0xB1, 0xB2, 0xB3,  # media next / prev / stop / play
    0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xAB, 0xAC,  # browser keys
    0x5F,  # sleep
})

_SPECIAL_QT_KEYS = {
    0x08: Qt.Key.Key_Backspace,
    0x09: Qt.Key.Key_Tab,
    0x0D: Qt.Key.Key_Return,
    0x1B: Qt.Key.Key_Escape,
    0x20: Qt.Key.Key_Space,
    0x21: Qt.Key.Key_PageUp,
    0x22: Qt.Key.Key_PageDown,
    0x23: Qt.Key.Key_End,
    0x24: Qt.Key.Key_Home,
    0x25: Qt.Key.Key_Left,
    0x26: Qt.Key.Key_Up,
    0x27: Qt.Key.Key_Right,
    0x28: Qt.Key.Key_Down,
    0x2C: Qt.Key.Key_Print,
    0x2D: Qt.Key.Key_Insert,
    0x2E: Qt.Key.Key_Delete,
    0x5B: Qt.Key.Key_Meta,
    0x5C: Qt.Key.Key_Meta,
    0x60: Qt.Key.Key_0,
    0x61: Qt.Key.Key_1,
    0x62: Qt.Key.Key_2,
    0x63: Qt.Key.Key_3,
    0x64: Qt.Key.Key_4,
    0x65: Qt.Key.Key_5,
    0x66: Qt.Key.Key_6,
    0x67: Qt.Key.Key_7,
    0x68: Qt.Key.Key_8,
    0x69: Qt.Key.Key_9,
    0x6A: Qt.Key.Key_Asterisk,
    0x6B: Qt.Key.Key_Plus,
    0x6D: Qt.Key.Key_Minus,
    0x6E: Qt.Key.Key_Period,
    0x6F: Qt.Key.Key_Slash,
    0xBA: Qt.Key.Key_Semicolon,
    0xBB: Qt.Key.Key_Equal,
    0xBC: Qt.Key.Key_Comma,
    0xBD: Qt.Key.Key_Minus,
    0xBE: Qt.Key.Key_Period,
    0xBF: Qt.Key.Key_Slash,
    0xC0: Qt.Key.Key_QuoteLeft,
    0xDB: Qt.Key.Key_BracketLeft,
    0xDC: Qt.Key.Key_Backslash,
    0xDD: Qt.Key.Key_BracketRight,
    0xDE: Qt.Key.Key_Apostrophe,
    0xE2: Qt.Key.Key_Backslash,
}

if _IS_WINDOWS:

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    _HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )


def _log(message: str) -> None:
    """Best-effort diagnostics (windowed builds have no stderr)."""
    if sys.stderr is not None:
        print(f"[input] {message}", file=sys.stderr)


def _qt_key_for(vk: int) -> int:
    if 0x41 <= vk <= 0x5A:  # A-Z
        return int(Qt.Key.Key_A) + (vk - 0x41)
    if 0x30 <= vk <= 0x39:  # 0-9
        return int(Qt.Key.Key_0) + (vk - 0x30)
    if 0x70 <= vk <= 0x87:  # F1-F24
        return int(Qt.Key.Key_F1) + (vk - 0x70)
    return int(_SPECIAL_QT_KEYS.get(vk, Qt.Key.Key_unknown))


class InputGate(QObject):
    """Global mouse/keyboard gate for a no-activate overlay window."""

    # qt_key, is_release, text, Qt modifiers, auto-repeat
    key_event = pyqtSignal(int, bool, str, int, bool)
    # angle_dx, angle_dy, global physical x, global physical y, Qt modifiers
    wheel_event = pyqtSignal(int, int, int, int, int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._enabled = False
        self._engaged = False
        self._engaged_fg = 0
        self._hwnd = 0
        self._swallowed: set[int] = set()
        self._passthrough: set[tuple[int, int]] = set()
        self._keyboard_hook = None
        self._mouse_hook = None
        self._keyboard_proc = None
        self._mouse_proc = None
        self._user32 = None
        # Synthetic input (e.g. the Ctrl+V sent after a screenshot) must reach
        # the background app untouched; automated tests flip this.
        self.accept_injected = False
        if _IS_WINDOWS:
            self._user32 = self._load_user32()

    # -- public API ---------------------------------------------------------

    @property
    def engaged(self) -> bool:
        return self._engaged

    def set_hwnd(self, hwnd: int) -> None:
        self._hwnd = int(hwnd or 0)

    def set_passthrough_hotkeys(
        self, combos: set[tuple[int, int]]
    ) -> None:
        """Hotkey (modifiers, vk) pairs that must reach RegisterHotKey."""
        self._passthrough = set(combos)

    def enable(self) -> None:
        """Install the hooks; harmless if already enabled or not Windows."""
        if not _IS_WINDOWS or self._enabled or self._user32 is None:
            return
        u32 = self._user32
        self._keyboard_proc = _HOOKPROC(self._keyboard_hook_proc)
        self._mouse_proc = _HOOKPROC(self._mouse_hook_proc)
        # Low-level hooks run in this process; the module handle must be NULL.
        self._keyboard_hook = u32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, self._keyboard_proc, None, 0
        )
        self._mouse_hook = u32.SetWindowsHookExW(
            _WH_MOUSE_LL, self._mouse_proc, None, 0
        )
        if not self._keyboard_hook or not self._mouse_hook:
            error = ctypes.get_last_error()
            _log(
                "could not install global input hooks "
                f"(error {error}); click-to-type will fall back to "
                "window activation."
            )
            self.disable()
            return
        self._enabled = True

    def disable(self) -> None:
        if self._user32 is not None:
            for hook in (self._keyboard_hook, self._mouse_hook):
                if hook:
                    self._user32.UnhookWindowsHookEx(hook)
        self._keyboard_hook = None
        self._mouse_hook = None
        self._keyboard_proc = None
        self._mouse_proc = None
        self._enabled = False
        self._engaged = False
        self._engaged_fg = 0
        self._swallowed.clear()

    def close(self) -> None:
        self.disable()

    # -- native plumbing ----------------------------------------------------

    def _load_user32(self):
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            _HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        u32.SetWindowsHookExW.restype = ctypes.c_void_p
        u32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        u32.CallNextHookEx.restype = ctypes.c_ssize_t
        u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u32.UnhookWindowsHookEx.restype = wintypes.BOOL
        u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u32.GetAsyncKeyState.restype = ctypes.c_short
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        u32.GetKeyboardLayout.restype = ctypes.c_void_p
        u32.ToUnicodeEx.argtypes = [
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(wintypes.BYTE),
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.UINT,
            ctypes.c_void_p,
        ]
        u32.ToUnicodeEx.restype = ctypes.c_int
        u32.WindowFromPoint.argtypes = [wintypes.POINT]
        u32.WindowFromPoint.restype = wintypes.HWND
        u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u32.GetAncestor.restype = wintypes.HWND
        return u32

    def _call_next(self, hook, n_code, w_param, l_param) -> int:
        return int(
            self._user32.CallNextHookEx(hook, n_code, w_param, l_param)
        )

    def _down(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _qt_modifiers(self) -> int:
        mods = 0
        if self._down(_VK_SHIFT):
            mods |= int(Qt.KeyboardModifier.ShiftModifier)
        if self._down(_VK_CONTROL):
            mods |= int(Qt.KeyboardModifier.ControlModifier)
        if self._down(_VK_MENU):
            mods |= int(Qt.KeyboardModifier.AltModifier)
        if self._down(_VK_LWIN) or self._down(_VK_RWIN):
            mods |= int(Qt.KeyboardModifier.MetaModifier)
        return mods

    def _raw_modifiers(self) -> int:
        mods = 0
        if self._down(_VK_SHIFT):
            mods |= MOD_SHIFT
        if self._down(_VK_CONTROL):
            mods |= MOD_CONTROL
        if self._down(_VK_MENU):
            mods |= MOD_ALT
        if self._down(_VK_LWIN) or self._down(_VK_RWIN):
            mods |= MOD_WIN
        return mods

    def _point_over_hud(self, x: int, y: int) -> bool:
        if not self._hwnd:
            return False
        window = int(
            self._user32.WindowFromPoint(wintypes.POINT(int(x), int(y))) or 0
        )
        if not window:
            return False
        root = int(self._user32.GetAncestor(window, _GA_ROOT) or 0)
        return root == self._hwnd

    # -- hooks --------------------------------------------------------------

    def _keyboard_hook_proc(self, n_code, w_param, l_param):
        if n_code != _HC_ACTION or not self._enabled:
            return self._call_next(
                self._keyboard_hook, n_code, w_param, l_param
            )
        try:
            if int(w_param) in _KEY_MESSAGES:
                data = ctypes.cast(
                    l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)
                ).contents
                if self._handle_key(data):
                    return 1
        except Exception as exc:  # noqa: BLE001 — never break the hook chain
            _log(f"keyboard hook error: {exc}")
        return self._call_next(
            self._keyboard_hook, n_code, w_param, l_param
        )

    def _mouse_hook_proc(self, n_code, w_param, l_param):
        if n_code != _HC_ACTION or not self._enabled:
            return self._call_next(self._mouse_hook, n_code, w_param, l_param)
        message = int(w_param)
        try:
            if message == _WM_MOUSEMOVE:
                return self._call_next(
                    self._mouse_hook, n_code, w_param, l_param
                )
            data = ctypes.cast(
                l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)
            ).contents
            if message in (_WM_MOUSEWHEEL, _WM_MOUSEHWHEEL):
                if self._point_over_hud(data.pt.x, data.pt.y):
                    delta = ctypes.c_short(
                        (int(data.mouseData) >> 16) & 0xFFFF
                    ).value
                    if message == _WM_MOUSEWHEEL:
                        dx, dy = 0, int(delta)
                    else:
                        dx, dy = int(delta), 0
                    self.wheel_event.emit(
                        dx, dy, int(data.pt.x), int(data.pt.y),
                        self._qt_modifiers(),
                    )
                    return 1
            elif message == _WM_LBUTTONDOWN:
                self._engaged = self._point_over_hud(
                    data.pt.x, data.pt.y
                )
                self._engaged_fg = (
                    int(self._user32.GetForegroundWindow() or 0)
                    if self._engaged
                    else 0
                )
        except Exception as exc:  # noqa: BLE001 — never break the hook chain
            _log(f"mouse hook error: {exc}")
        return self._call_next(self._mouse_hook, n_code, w_param, l_param)

    # -- key classification -------------------------------------------------

    def _handle_key(self, data) -> bool:
        vk = int(data.vkCode)
        flags = int(data.flags)
        scan = int(data.scanCode)
        is_up = bool(flags & _LLKHF_UP)

        if is_up and vk in self._swallowed:
            self._swallowed.discard(vk)
            qt_key = _qt_key_for(vk)
            if qt_key != Qt.Key.Key_unknown:
                self.key_event.emit(
                    qt_key, True, "", self._qt_modifiers(), False
                )
            return True

        if flags & _LLKHF_INJECTED and not self.accept_injected:
            return False
        if vk in _MODIFIER_VKS or vk in _ALWAYS_PASS_VKS:
            return False
        if not self._engaged:
            return False

        raw_mods = self._raw_modifiers()
        if raw_mods & MOD_WIN:
            return False
        if (raw_mods, vk) in self._passthrough:
            return False
        # Switching apps (Alt+Tab, focus-follows-mouse, a dialog popping up)
        # releases capture instead of silently swallowing keys meant for the
        # new foreground window.
        if self._engaged_fg and int(
            self._user32.GetForegroundWindow() or 0
        ) != self._engaged_fg:
            self._engaged = False
            self._engaged_fg = 0
            self._swallowed.clear()
            return False

        qt_key, text = self._translate(vk, scan)
        if raw_mods & MOD_ALT and not text:
            return False
        if vk == _VK_PROCESSKEY or vk == _VK_PACKET:
            self._swallowed.add(vk)
            return True
        if qt_key == Qt.Key.Key_unknown and not text:
            return False

        repeat = vk in self._swallowed
        self._swallowed.add(vk)
        self.key_event.emit(
            qt_key, is_up, text, self._qt_modifiers(), repeat
        )
        return True

    def _translate(self, vk: int, scan: int) -> tuple[int, str]:
        u32 = self._user32
        state = (wintypes.BYTE * 256)()
        for code in (
            _VK_SHIFT, _VK_LSHIFT, _VK_RSHIFT,
            _VK_CONTROL, _VK_LCONTROL, _VK_RCONTROL,
            _VK_MENU, _VK_LMENU, _VK_RMENU,
        ):
            if self._down(code):
                state[code] = 0x80
        if u32.GetAsyncKeyState(_VK_CAPITAL) & 0x0001:
            state[_VK_CAPITAL] = 0x01

        foreground = int(u32.GetForegroundWindow() or 0)
        thread = int(u32.GetWindowThreadProcessId(foreground, None) or 0)
        layout = u32.GetKeyboardLayout(thread)

        buffer = ctypes.create_unicode_buffer(8)
        count = u32.ToUnicodeEx(
            vk, scan, state, buffer, len(buffer), 0x4, layout
        )
        text = ""
        if count > 0:
            text = "".join(
                ch for ch in buffer.value[:count]
                if ch >= " " and ch != "\x7f"
            )
        return _qt_key_for(vk), text
