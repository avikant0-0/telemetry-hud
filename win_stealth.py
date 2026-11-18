#!/usr/bin/env python3
"""Windows-specific window stealth for the telemetry HUD.

Two independent mechanisms live here:

* Capture exclusion through ``SetWindowDisplayAffinity``, which keeps the
  window out of DWM-based screen capture and recording paths
  (``WDA_EXCLUDEFROMCAPTURE``, Windows 10 2004+) or masks it as a black
  rectangle (``WDA_MONITOR``, the fallback for older builds and tools that
  only understand affinity value 1).
* Input-transparency handling through extended window styles, so the window
  either ignores mouse input entirely or accepts mouse input without ever
  becoming the foreground window. `WS_EX_NOACTIVATE` and `WS_EX_TOOLWINDOW`
  are kept in both modes, so the overlay never steals focus and never appears
  in Alt+Tab or the taskbar.

On non-Windows platforms the helpers return a report describing that nothing
was changed.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

if sys.platform == "win32":
    from ctypes import wintypes

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011

_CAPTURE_VALUES = {
    "off": WDA_NONE,
    "monitor": WDA_MONITOR,
    "exclude": WDA_EXCLUDEFROMCAPTURE,
}
CAPTURE_MODES = tuple(_CAPTURE_VALUES)

_EXCLUDE_MIN_BUILD = 19041

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

_user32: ctypes.WinDLL | None = None


@dataclass(frozen=True)
class StealthReport:
    capture_mode: str
    capture_affinity: int | None
    capture_ok: bool
    input_transparent: bool
    input_ok: bool
    messages: tuple[str, ...]


def apply_window_stealth(
    hwnd: int,
    capture: str = "exclude",
    input_transparent: bool = True,
) -> StealthReport:
    """Apply capture exclusion and the requested input mode to a top-level window."""
    if capture not in _CAPTURE_VALUES:
        raise ValueError(
            f"capture must be one of {', '.join(CAPTURE_MODES)}; got {capture!r}"
        )
    if sys.platform != "win32":
        return StealthReport(
            capture_mode=capture,
            capture_affinity=None,
            capture_ok=False,
            input_transparent=input_transparent,
            input_ok=False,
            messages=("window stealth is Windows-only; no native changes applied",),
        )

    messages: list[str] = []
    u32 = _get_user32()
    affinity, capture_ok = _apply_capture_affinity(u32, hwnd, capture, messages)
    input_ok = _set_input_transparency(u32, hwnd, input_transparent, messages)
    return StealthReport(
        capture_mode=capture,
        capture_affinity=affinity,
        capture_ok=capture_ok,
        input_transparent=input_transparent,
        input_ok=input_ok,
        messages=tuple(messages),
    )


def set_input_transparency(
    hwnd: int, transparent: bool
) -> tuple[bool, tuple[str, ...]]:
    """Toggle native input transparency at runtime.

    Returns ``(ok, messages)``. The window is always kept non-activating
    (``WS_EX_NOACTIVATE``) while staying out of Alt+Tab/taskbar; when
    *transparent* is False, mouse clicks are delivered to the overlay.
    """
    if sys.platform != "win32":
        return False, ("input transparency is Windows-only; no native changes applied",)
    messages: list[str] = []
    ok = _set_input_transparency(_get_user32(), hwnd, transparent, messages)
    return ok, tuple(messages)


def _get_user32() -> ctypes.WinDLL:
    global _user32
    if _user32 is None:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        u32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        u32.GetWindowDisplayAffinity.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        u32.GetWindowDisplayAffinity.restype = wintypes.BOOL
        u32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        u32.SetWindowPos.restype = wintypes.BOOL
        _user32 = u32
    return _user32


def _apply_capture_affinity(
    u32: ctypes.WinDLL,
    hwnd: int,
    mode: str,
    messages: list[str],
) -> tuple[int | None, bool]:
    if mode == "exclude":
        build = sys.getwindowsversion().build
        if build < _EXCLUDE_MIN_BUILD:
            messages.append(
                "WDA_EXCLUDEFROMCAPTURE requires Windows 10 2004 (build "
                f"{_EXCLUDE_MIN_BUILD}) or newer, but this system is build "
                f"{build}; use --capture-exclusion monitor to black the window "
                "out in captures, or --capture-exclusion off to disable."
            )
            return None, False

    requested = _CAPTURE_VALUES[mode]
    ctypes.set_last_error(0)
    if not u32.SetWindowDisplayAffinity(hwnd, requested):
        error = ctypes.get_last_error()
        detail = ctypes.FormatError(error).strip() if error else "unknown error"
        messages.append(
            f"SetWindowDisplayAffinity({mode}) failed: {detail} "
            f"(error {error}); the window may still appear in screen captures."
        )
        return None, False

    actual = wintypes.DWORD()
    if not u32.GetWindowDisplayAffinity(hwnd, ctypes.byref(actual)):
        error = ctypes.get_last_error()
        messages.append(
            "SetWindowDisplayAffinity succeeded but GetWindowDisplayAffinity "
            f"failed while verifying it (error {error})."
        )
        return requested, False
    if actual.value != requested:
        messages.append(
            "capture affinity verification mismatch: requested "
            f"{requested:#04x}, got {actual.value:#04x}."
        )
        return actual.value, False

    if mode == "off":
        messages.append("capture exclusion disabled (WDA_NONE).")
    elif mode == "exclude":
        messages.append(
            "capture exclusion active (WDA_EXCLUDEFROMCAPTURE): DWM-based "
            "screen capture and recording will omit this window."
        )
    else:
        messages.append(
            "capture masking active (WDA_MONITOR): this window appears as a "
            "black rectangle in captures."
        )
    return requested, True


def _set_input_transparency(
    u32: ctypes.WinDLL,
    hwnd: int,
    transparent: bool,
    messages: list[str],
) -> bool:
    get_long = getattr(u32, "GetWindowLongPtrW", None) or u32.GetWindowLongW
    set_long = getattr(u32, "SetWindowLongPtrW", None) or u32.SetWindowLongW
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_long.restype = ctypes.c_ssize_t

    ctypes.set_last_error(0)
    current = int(get_long(hwnd, _GWL_EXSTYLE))
    # WS_EX_NOACTIVATE is always kept: clicks on the overlay must never move
    # the foreground window away from the app underneath. Only the
    # click-through bit changes between modes.
    desired = current | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
    if transparent:
        desired |= _WS_EX_TRANSPARENT
    else:
        desired &= ~_WS_EX_TRANSPARENT

    if desired != current:
        ctypes.set_last_error(0)
        previous = int(set_long(hwnd, _GWL_EXSTYLE, desired))
        if previous == 0 and ctypes.get_last_error() != 0:
            error = ctypes.get_last_error()
            messages.append(
                "could not update the extended window styles "
                f"(error {error}); input transparency may be incorrect."
            )
            return False
        u32.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            _SWP_NOSIZE
            | _SWP_NOMOVE
            | _SWP_NOZORDER
            | _SWP_NOACTIVATE
            | _SWP_FRAMECHANGED,
        )

    styles = int(get_long(hwnd, _GWL_EXSTYLE))
    required = _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE | (
        _WS_EX_TRANSPARENT if transparent else 0
    )
    actual = styles & (
        _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE | _WS_EX_TRANSPARENT
    )
    if actual == required:
        if transparent:
            messages.append(
                "input transparency active (WS_EX_TRANSPARENT | "
                "WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW): clicks and keys pass "
                "through to the app underneath."
            )
        else:
            messages.append(
                "overlay stays non-activating (WS_EX_NOACTIVATE | "
                "WS_EX_TOOLWINDOW): clicks reach the HUD without changing "
                "the foreground window."
            )
        return True

    messages.append(
        "input transparency is in the wrong state: expected "
        f"{required:#010x}, got {actual:#010x}."
    )
    return False
