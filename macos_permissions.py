"""Small, dependency-free macOS privacy permission checks."""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys


logger = logging.getLogger("ProjectX")


def _screen_capture_access(request: bool) -> bool:
    core_graphics = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    core_graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    allowed = bool(core_graphics.CGPreflightScreenCaptureAccess())
    if not allowed and request:
        core_graphics.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        allowed = bool(core_graphics.CGRequestScreenCaptureAccess())
    return allowed


def _accessibility_access() -> bool:
    application_services = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    application_services.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(application_services.AXIsProcessTrusted())


def check_macos_permissions(
    request: bool = False,
    open_settings: bool = False,
) -> dict[str, bool]:
    """Check permissions needed for capture and remote input.

    Screen Recording has a native request API. Accessibility does not expose the
    same simple prompt through ctypes, so the appropriate System Settings pane is
    opened once when requested.
    """
    if sys.platform != "darwin":
        return {"screen_recording": True, "accessibility": True}

    screen_recording = _screen_capture_access(request=request)
    accessibility = _accessibility_access()

    if not accessibility and open_settings:
        logger.warning(
            "Accessibility permission is required for remote mouse and keyboard input."
        )
        try:
            subprocess.Popen(
                [
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    if not screen_recording:
        logger.warning(
            "Screen Recording permission is not enabled. Grant it to Project X "
            "in System Settings > Privacy & Security, then restart Project X."
        )
    if not accessibility:
        logger.warning(
            "After enabling Accessibility for Project X, restart Project X."
        )

    return {
        "screen_recording": screen_recording,
        "accessibility": accessibility,
    }
