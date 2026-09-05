"""Low-overhead native macOS input injection using CoreGraphics events."""

from __future__ import annotations

import Quartz


_MOUSE_BUTTONS = {
    "left": (
        Quartz.kCGMouseButtonLeft,
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
    ),
    "right": (
        Quartz.kCGMouseButtonRight,
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
    ),
    "middle": (
        Quartz.kCGMouseButtonCenter,
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
    ),
}

# Hardware key codes for the standard macOS ANSI layout. Text injection keeps
# using the layout-aware pynput path; these codes cover interactive shortcuts.
_KEY_CODES = {
    "KeyA": 0, "KeyS": 1, "KeyD": 2, "KeyF": 3, "KeyH": 4,
    "KeyG": 5, "KeyZ": 6, "KeyX": 7, "KeyC": 8, "KeyV": 9,
    "KeyB": 11, "KeyQ": 12, "KeyW": 13, "KeyE": 14, "KeyR": 15,
    "KeyY": 16, "KeyT": 17, "Digit1": 18, "Digit2": 19,
    "Digit3": 20, "Digit4": 21, "Digit6": 22, "Digit5": 23,
    "Equal": 24, "Digit9": 25, "Digit7": 26, "Minus": 27,
    "Digit8": 28, "Digit0": 29, "BracketRight": 30, "KeyO": 31,
    "KeyU": 32, "BracketLeft": 33, "KeyI": 34, "KeyP": 35,
    "Enter": 36, "KeyL": 37, "KeyJ": 38, "Quote": 39,
    "KeyK": 40, "Semicolon": 41, "Backslash": 42, "Comma": 43,
    "Slash": 44, "KeyN": 45, "KeyM": 46, "Period": 47,
    "Tab": 48, "Space": 49, "Backquote": 50, "Backspace": 51,
    "Escape": 53, "MetaLeft": 55, "ShiftLeft": 56, "CapsLock": 57,
    "AltLeft": 58, "ControlLeft": 59, "ShiftRight": 60,
    "AltRight": 61, "ControlRight": 62, "MetaRight": 54,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96,
    "F6": 97, "F7": 98, "F8": 100, "F9": 101, "F10": 109,
    "F11": 103, "F12": 111, "Home": 115, "End": 119,
    "PageUp": 116, "PageDown": 121, "Delete": 117,
    "ArrowLeft": 123, "ArrowRight": 124, "ArrowDown": 125,
    "ArrowUp": 126,
}


def move(x: int, y: int) -> None:
    Quartz.CGWarpMouseCursorPosition((x, y))


def mouse_button(name: str, pressed: bool, click_count: int = 1) -> None:
    button, down_type, up_type = _MOUSE_BUTTONS.get(name, _MOUSE_BUTTONS["left"])
    source_event = Quartz.CGEventCreate(None)
    location = Quartz.CGEventGetLocation(source_event)
    event = Quartz.CGEventCreateMouseEvent(
        None,
        down_type if pressed else up_type,
        location,
        button,
    )
    if click_count > 1:
        Quartz.CGEventSetIntegerValueField(
            event,
            Quartz.kCGMouseEventClickState,
            click_count,
        )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def click(name: str = "left", count: int = 1) -> None:
    mouse_button(name, True, count)
    mouse_button(name, False, count)


def scroll(delta_y: float) -> None:
    amount = max(-120, min(120, round(-delta_y)))
    event = Quartz.CGEventCreateScrollWheelEvent(
        None,
        Quartz.kCGScrollEventUnitPixel,
        1,
        amount,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def key(code: str, pressed: bool) -> bool:
    key_code = _KEY_CODES.get(code)
    if key_code is None:
        return False
    event = Quartz.CGEventCreateKeyboardEvent(None, key_code, pressed)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return True
