"""Raw desktop input backends for ``desktop.raw_input`` (always UNKNOWN_MUTATION: owner approval first).

* Windows: pywinauto (reference S06 for interactive-session caveats).
* macOS: pyautogui on Quartz events; needs the Accessibility permission for the process that runs
  the worker (System Settings > Privacy & Security > Accessibility) and an awake, unlocked screen.
* Linux: not supported.

Key chords use one platform-neutral notation, e.g. ``Control+s``, ``Meta+Shift+4``, ``Enter``.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Protocol

from ..contracts import ErrorCode
from .agent import JobFailure

SPECIAL = {"enter": "enter", "return": "enter", "tab": "tab", "escape": "esc", "esc": "esc", "delete": "delete",
           "backspace": "backspace", "space": "space", "up": "up", "down": "down", "left": "left", "right": "right",
           "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right", "home": "home",
           "end": "end", "pageup": "pageup", "pagedown": "pagedown"}
MODIFIERS = {"control": "ctrl", "ctrl": "ctrl", "shift": "shift", "alt": "alt", "option": "alt", "meta": "meta",
             "cmd": "meta", "command": "meta", "win": "meta", "super": "meta"}


def parse_chord(keys: str) -> tuple[list[str], str]:
    parts = [p.strip() for p in keys.split("+") if p.strip()]
    if not parts:
        raise JobFailure(ErrorCode.MALFORMED_TOOL_CALL, "empty key chord")
    mods = []
    for p in parts[:-1]:
        m = MODIFIERS.get(p.lower())
        if m is None:
            raise JobFailure(ErrorCode.MALFORMED_TOOL_CALL, f"unknown modifier {p!r}")
        mods.append(m)
    key = parts[-1]
    low = key.lower()
    if low in SPECIAL:
        key = SPECIAL[low]
    elif len(key) == 1 or (low.startswith("f") and low[1:].isdigit()):
        key = low
    else:
        raise JobFailure(ErrorCode.MALFORMED_TOOL_CALL, f"unknown key {key!r}")
    return mods, key


def pywinauto_chord(keys: str) -> str:
    mods, key = parse_chord(keys)
    prefix = "".join({"ctrl": "^", "shift": "+", "alt": "%"}.get(m, "") for m in mods)
    if "meta" in mods:
        raise JobFailure(ErrorCode.UNSUPPORTED_CAPABILITY, "the Windows key is not supported in raw input")
    named = {"enter": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}", "delete": "{DEL}", "backspace": "{BACKSPACE}",
             "space": " ", "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}", "home": "{HOME}",
             "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}"}
    if key in named:
        return prefix + named[key]
    if key.startswith("f") and key[1:].isdigit():
        return prefix + "{" + key.upper() + "}"
    return prefix + key


def pyautogui_chord(keys: str) -> list[str]:
    mods, key = parse_chord(keys)
    return [{"meta": "command", "alt": "option"}.get(m, m) for m in mods] + [key]


class DesktopBackend(Protocol):
    def click(self, x: int, y: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def hotkey(self, keys: str) -> None: ...
    def scroll(self, x: int, y: int) -> None: ...


class WindowsBackend:
    def __init__(self) -> None:
        try:
            from pywinauto import keyboard, mouse  # type: ignore[import-not-found]
        except ImportError as exc:
            raise JobFailure(ErrorCode.UNSUPPORTED_CAPABILITY, "pywinauto is not installed (pip install omca[desktop])") from exc
        self.keyboard, self.mouse = keyboard, mouse

    def click(self, x: int, y: int) -> None:
        self.mouse.click(coords=(x, y))

    def type_text(self, text: str) -> None:
        self.keyboard.send_keys(text, with_spaces=True, pause=0.02)

    def hotkey(self, keys: str) -> None:
        self.keyboard.send_keys(pywinauto_chord(keys))

    def scroll(self, x: int, y: int) -> None:
        self.mouse.scroll(coords=(x, y), wheel_dist=-3)


def macos_accessibility_trusted() -> bool:
    try:
        lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except OSError:
        return False


class MacBackend:
    def __init__(self) -> None:
        if not macos_accessibility_trusted():
            raise JobFailure(ErrorCode.AUTH_REQUIRED,
                             "macOS Accessibility permission is missing for the worker process "
                             "(System Settings > Privacy & Security > Accessibility)")
        try:
            import pyautogui  # type: ignore[import-not-found]
        except ImportError as exc:
            raise JobFailure(ErrorCode.UNSUPPORTED_CAPABILITY, "pyautogui is not installed (pip install omca[desktop])") from exc
        pyautogui.FAILSAFE = True  # moving the pointer to a screen corner aborts input
        self.p = pyautogui

    def click(self, x: int, y: int) -> None:
        self.p.click(x, y)

    def type_text(self, text: str) -> None:
        self.p.write(text, interval=0.02)

    def hotkey(self, keys: str) -> None:
        self.p.hotkey(*pyautogui_chord(keys))

    def scroll(self, x: int, y: int) -> None:
        self.p.scroll(-3, x=x, y=y)


def backend_for_platform(platform: str | None = None) -> DesktopBackend:
    platform = platform or sys.platform
    if platform == "win32":
        return WindowsBackend()
    if platform == "darwin":
        return MacBackend()
    raise JobFailure(ErrorCode.UNSUPPORTED_CAPABILITY, "raw desktop input is only available on Windows and macOS workers")
