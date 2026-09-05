"""Platform screen-capture backends used by the remote host."""

from __future__ import annotations

from dataclasses import dataclass
import sys
import threading
from typing import Any

import numpy as np
from mss import MSS
from PIL import Image
import simplejpeg


@dataclass(frozen=True, slots=True)
class MonitorGeometry:
    id: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False
    name: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "is_primary": self.is_primary,
            "name": self.name or f"Screen {self.id}",
        }


def _monitor_value(monitor: Any, key: str, default: Any = None) -> Any:
    """Support both MSS 10 mapping-style and MSS 11 attribute-style monitors."""
    value = getattr(monitor, key, None)
    if value is not None:
        return value
    try:
        return monitor[key]
    except (KeyError, TypeError):
        return default


class MSSScreenCapture:
    """Cross-platform monitor discovery and reliable MSS capture fallback.

    MSS instances are thread-local because captures run via ``asyncio.to_thread``.
    This also keeps the backend compatible with MSS releases predating its shared
    instance cross-thread guarantees.
    """

    def __init__(self, include_cursor: bool = True):
        self.include_cursor = include_cursor
        self._thread_local = threading.local()
        # CoreGraphics capture can stall under concurrent grabs. Serializing only
        # the OS capture call keeps latency predictable; resize/JPEG work remains
        # concurrent in each worker thread.
        self._grab_lock = threading.Lock()
        self._monitors: dict[int, MonitorGeometry] = {}
        self.refresh_monitors()

    def _new_capture(self) -> MSS:
        return MSS(with_cursor=self.include_cursor)

    def _capture_for_current_thread(self) -> MSS:
        capture = getattr(self._thread_local, "capture", None)
        if capture is None:
            capture = self._new_capture()
            self._thread_local.capture = capture
        return capture

    def refresh_monitors(self) -> list[MonitorGeometry]:
        with self._new_capture() as capture:
            primary = capture.primary_monitor
            primary_id = None
            monitors: dict[int, MonitorGeometry] = {}
            for monitor_id, monitor in enumerate(capture.monitors[1:], start=1):
                left = int(_monitor_value(monitor, "left", 0))
                top = int(_monitor_value(monitor, "top", 0))
                width = int(_monitor_value(monitor, "width", 0))
                height = int(_monitor_value(monitor, "height", 0))
                is_primary = bool(_monitor_value(monitor, "is_primary", False))
                if monitor is primary:
                    is_primary = True
                if is_primary:
                    primary_id = monitor_id
                monitors[monitor_id] = MonitorGeometry(
                    id=monitor_id,
                    left=left,
                    top=top,
                    width=width,
                    height=height,
                    is_primary=is_primary,
                    name=_monitor_value(monitor, "name"),
                )

            if monitors and primary_id is None:
                first_id = next(iter(monitors))
                first = monitors[first_id]
                monitors[first_id] = MonitorGeometry(
                    id=first.id,
                    left=first.left,
                    top=first.top,
                    width=first.width,
                    height=first.height,
                    is_primary=True,
                    name=first.name,
                )

        if not monitors:
            raise RuntimeError("No displays were detected by the screen-capture backend")
        self._monitors = monitors
        return list(monitors.values())

    def monitors(self, refresh: bool = False) -> list[MonitorGeometry]:
        if refresh:
            return self.refresh_monitors()
        return list(self._monitors.values())

    def monitor(self, monitor_id: int) -> MonitorGeometry:
        monitor = self._monitors.get(monitor_id)
        if monitor is None:
            monitor = self.refresh_monitors()[0]
        return monitor

    def primary_monitor_id(self) -> int:
        for monitor in self._monitors.values():
            if monitor.is_primary:
                return monitor.id
        return next(iter(self._monitors))

    def grab_jpeg(
        self,
        monitor_id: int,
        quality: int = 55,
        scale: float = 0.85,
    ) -> tuple[bytes | None, MonitorGeometry]:
        frame, geometry = self.grab_bgra(monitor_id)

        if scale < 0.995:
            image = Image.frombuffer(
                "RGBA",
                (frame.shape[1], frame.shape[0]),
                frame,
                "raw",
                "BGRA",
                0,
                1,
            ).convert("RGB")
            output_width = max(320, round(frame.shape[1] * scale))
            output_height = max(240, round(frame.shape[0] * scale))
            image = image.resize((output_width, output_height), Image.Resampling.BILINEAR)
            encode_frame = np.asarray(image)
            colorspace = "RGB"
        else:
            encode_frame = frame
            colorspace = "BGRA"
        return simplejpeg.encode_jpeg(
            encode_frame,
            quality=int(quality),
            colorspace=colorspace,
            colorsubsampling="420",
            fastdct=True,
        ), geometry

    def grab_bgra(self, monitor_id: int) -> tuple[np.ndarray, MonitorGeometry]:
        """Capture a raw BGRA frame for shared media pipelines."""
        capture = self._capture_for_current_thread()
        geometry = self.monitor(monitor_id)
        try:
            mss_monitor = capture.monitors[geometry.id]
        except IndexError:
            self.refresh_monitors()
            geometry = self.monitor(self.primary_monitor_id())
            mss_monitor = capture.monitors[geometry.id]
        with self._grab_lock:
            screenshot = capture.grab(mss_monitor)
        return np.asarray(screenshot), geometry


def create_screen_capture(include_cursor: bool = True) -> MSSScreenCapture:
    if sys.platform not in {"win32", "darwin"}:
        raise RuntimeError(
            f"Unsupported host platform {sys.platform!r}; Windows and macOS are supported"
        )
    return MSSScreenCapture(include_cursor=include_cursor)
