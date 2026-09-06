"""Shared latest-frame capture using ScreenCaptureKit, DXcam, or MSS."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import ctypes
from dataclasses import dataclass

import numpy as np
from PIL import Image
import simplejpeg

try:
    import av
    from av.video.reformatter import Interpolation
except ImportError:  # pragma: no cover - PyAV is installed with WebRTC extras.
    av = None
    Interpolation = None

from capture_backends import MonitorGeometry


logger = logging.getLogger("app")

DXCAM_AVAILABLE = False
if sys.platform == "win32":
    try:
        import dxcam

        DXCAM_AVAILABLE = True
    except ImportError:
        dxcam = None


class _VImageBuffer(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("height", ctypes.c_ulong),
        ("width", ctypes.c_ulong),
        ("row_bytes", ctypes.c_size_t),
    ]


def _load_vimage_scaler():
    """Load Apple's SIMD-accelerated scaler without importing another FFmpeg."""
    if sys.platform != "darwin":
        return None
    try:
        accelerate = ctypes.CDLL(
            "/System/Library/Frameworks/Accelerate.framework/Accelerate"
        )
        scaler = accelerate.vImageScale_ARGB8888
        scaler.argtypes = [
            ctypes.POINTER(_VImageBuffer),
            ctypes.POINTER(_VImageBuffer),
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        scaler.restype = ctypes.c_long
        return scaler
    except (AttributeError, OSError):
        return None


_VIMAGE_SCALE = _load_vimage_scaler()
_VIMAGE_HIGH_QUALITY = 32


def _scale_bgra(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Scale BGRA pixels with the fastest safe native path available."""
    # vImage accepts a row stride, so a CVPixelBuffer view with padding at the
    # end of each row does not need to be copied to a contiguous temporary.
    # Only require the channel stride to be one byte; this keeps the native
    # SIMD path available for ScreenCaptureKit's usual padded buffers.
    if (
        _VIMAGE_SCALE is not None
        and frame.dtype == np.uint8
        and frame.ndim == 3
        and frame.shape[2] == 4
        and frame.strides[2] == 1
    ):
        output = np.empty((height, width, 4), dtype=np.uint8)
        source = _VImageBuffer(
            frame.ctypes.data,
            frame.shape[0],
            frame.shape[1],
            frame.strides[0],
        )
        destination = _VImageBuffer(
            output.ctypes.data,
            output.shape[0],
            output.shape[1],
            output.strides[0],
        )
        error = _VIMAGE_SCALE(
            ctypes.byref(source),
            ctypes.byref(destination),
            None,
            _VIMAGE_HIGH_QUALITY,
        )
        if error == 0:
            return output
        logger.warning("vImage scaling failed (%d); trying cross-platform scaler", error)

    if av is not None:
        try:
            video_frame = av.VideoFrame.from_ndarray(frame, format="bgra")
            scaled = video_frame.reformat(
                width=width,
                height=height,
                format="bgra",
                interpolation=Interpolation.BILINEAR,
            )
            return scaled.to_ndarray(format="bgra")
        except (ValueError, RuntimeError) as error:
            logger.debug("PyAV scaling unavailable; using Pillow: %s", error)

    image = Image.frombuffer(
        "RGBA",
        (frame.shape[1], frame.shape[0]),
        frame,
        "raw",
        "BGRA",
        0,
        1,
    )
    return np.asarray(
        image.resize((width, height), Image.Resampling.BILINEAR)
    )[:, :, [2, 1, 0, 3]]


@dataclass(slots=True)
class FrameSnapshot:
    sequence: int
    captured_at: float
    bgra: np.ndarray
    monitor: MonitorGeometry


class _LatestFrameStore:
    """Double-buffered latest-frame state shared by every media consumer."""

    def __init__(self, monitor: MonitorGeometry):
        self.monitor = monitor
        self.condition = threading.Condition()
        self.sequence = 0
        self.captured_at = 0.0
        self.buffers: list[np.ndarray | None] = [None, None]
        self.active_buffer = 0
        self.jpeg_cache: dict[tuple[int, int, int], bytes] = {}
        self.frames_published = 0
        self.copy_total_ms = 0.0
        self.copy_max_ms = 0.0

    def publish(self, frame: np.ndarray) -> None:
        started = time.perf_counter()
        with self.condition:
            target = 1 - self.active_buffer
            if self.buffers[target] is None or self.buffers[target].shape != frame.shape:
                self.buffers[target] = np.empty_like(frame)
            np.copyto(self.buffers[target], frame)
            self.active_buffer = target
            self.sequence += 1
            self.captured_at = time.perf_counter()
            copy_ms = (self.captured_at - started) * 1000
            self.frames_published += 1
            self.copy_total_ms += copy_ms
            self.copy_max_ms = max(self.copy_max_ms, copy_ms)
            self.jpeg_cache.clear()
            self.condition.notify_all()

    def metrics(self):
        with self.condition:
            frames = self.frames_published
            return {
                "frames": frames,
                "copy_avg_ms": round(self.copy_total_ms / max(1, frames), 3),
                "copy_max_ms": round(self.copy_max_ms, 3),
                "sequence": self.sequence,
                "frame_age_ms": round(
                    max(0.0, time.perf_counter() - self.captured_at) * 1000,
                    1,
                ) if self.captured_at else None,
            }

    def wait(self, after_sequence: int = 0, timeout: float = 1.0) -> FrameSnapshot | None:
        with self.condition:
            if self.sequence <= after_sequence:
                self.condition.wait_for(
                    lambda: self.sequence > after_sequence,
                    timeout=timeout,
                )
            frame = self.buffers[self.active_buffer]
            if frame is None or self.sequence <= after_sequence:
                return None
            return FrameSnapshot(
                sequence=self.sequence,
                captured_at=self.captured_at,
                bgra=frame,
                monitor=self.monitor,
            )

    def jpeg(self, snapshot: FrameSnapshot, quality: int, scale: float) -> bytes | None:
        scale_key = round(scale * 1000)
        cache_key = (snapshot.sequence, quality, scale_key)
        with self.condition:
            cached = self.jpeg_cache.get(cache_key)
        if cached is not None:
            return cached

        frame = snapshot.bgra
        if scale < 0.995:
            width = max(320, round(frame.shape[1] * scale))
            height = max(240, round(frame.shape[0] * scale))
            encode_frame = _scale_bgra(frame, width, height)
            colorspace = "BGRA"
        else:
            encode_frame = frame
            colorspace = "BGRA"
        result = simplejpeg.encode_jpeg(
            encode_frame,
            quality=int(quality),
            colorspace=colorspace,
            colorsubsampling="420",
            fastdct=True,
        )
        with self.condition:
            if self.sequence == snapshot.sequence:
                self.jpeg_cache[cache_key] = result
        return result


class _MSSPollingStream:
    def __init__(self, capturer, monitor: MonitorGeometry, fps: int):
        self.capturer = capturer
        self.monitor = monitor
        self.fps = fps
        self.store = _LatestFrameStore(monitor)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"worker-{monitor.id}",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self):
        interval = 1.0 / self.fps
        while not self.stop_event.is_set():
            started = time.perf_counter()
            try:
                frame, _ = self.capturer.grab_bgra(self.monitor.id)
                self.store.publish(frame)
            except Exception as error:
                logger.error("Shared MSS capture failed: %s", error)
                self.stop_event.wait(0.25)
            elapsed = time.perf_counter() - started
            self.stop_event.wait(max(0.001, interval - elapsed))


class _DXCamPollingStream:
    """Low-latency Windows capture using DXGI/WGC and a two-frame ring.

    DXGI Desktop Duplication is the default because Windows Graphics Capture
    draws a system yellow privacy border. WinRT remains opt-in for systems
    where DXGI cannot capture a particular display.
    """

    def __init__(
        self,
        monitor: MonitorGeometry,
        fps: int,
        backend_preference: str | None = None,
    ):
        self.monitor = monitor
        self.fps = fps
        self.store = _LatestFrameStore(monitor)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"render-{monitor.id}",
            daemon=True,
        )
        self.camera = None
        self.backend = None
        preference = (
            backend_preference
            or os.environ.get("PROJECTX_WINDOWS_CAPTURE_BACKEND", "dxgi")
        ).strip().lower()
        self.backends = (
            ("winrt", "dxgi")
            if preference == "winrt"
            else ("dxgi",)
        )

    def _create_camera(self, backend):
        options = {
            "backend": backend,
            "device_idx": 0,
            "output_idx": max(0, self.monitor.id - 1),
            "output_color": "BGRA",
            "processor_backend": "numpy",
            "max_buffer_len": 2,
        }
        try:
            return dxcam.create(**options)
        except TypeError:
            # Preserve compatibility with DXcam releases before the explicit
            # NumPy processor backend option.
            options.pop("processor_backend", None)
            return dxcam.create(**options)

    def start(self):
        errors = []
        for backend in self.backends:
            camera = None
            try:
                camera = self._create_camera(backend)
                camera.start(target_fps=self.fps, video_mode=True)
                self.camera = camera
                self.backend = backend
                self.thread.start()
                logger.info(
                    "Windows %s capture started for monitor %d at %d FPS.",
                    backend.upper(),
                    self.monitor.id,
                    self.fps,
                )
                return
            except Exception as error:
                errors.append(f"{backend}: {error}")
                if camera is not None:
                    try:
                        camera.stop()
                    except Exception:
                        pass
        raise RuntimeError("; ".join(errors) or "DXcam could not start")

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        if self.camera is not None:
            try:
                self.camera.stop()
            except Exception:
                pass
            release = getattr(self.camera, "release", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    pass
        self.camera = None

    def _run(self):
        interval = 1.0 / self.fps
        while not self.stop_event.is_set():
            started = time.perf_counter()
            try:
                frame = self.camera.get_latest_frame()
                if frame is not None:
                    frame = np.asarray(frame)
                    if frame.ndim != 3 or frame.shape[2] != 4:
                        raise RuntimeError(
                            f"DXcam returned unexpected frame shape {frame.shape}"
                        )
                    self.store.publish(frame)
            except Exception as error:
                logger.error("Shared DXcam capture failed: %s", error)
                self.stop_event.wait(0.1)
            elapsed = time.perf_counter() - started
            self.stop_event.wait(max(0.001, interval - elapsed))


if sys.platform == "darwin":
    import objc
    import Quartz
    from CoreMedia import (
        CMTimeMake,
        CMSampleBufferGetImageBuffer,
        CMSampleBufferGetSampleAttachmentsArray,
    )
    from Foundation import NSObject
    from ScreenCaptureKit import (
        SCContentFilter,
        SCShareableContent,
        SCFrameStatusComplete,
        SCFrameStatusIdle,
        SCStream,
        SCStreamConfiguration,
        SCStreamFrameInfoStatus,
        SCStreamOutputTypeScreen,
    )

    _libdispatch = ctypes.CDLL("/usr/lib/system/libdispatch.dylib")
    _libdispatch.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    _libdispatch.dispatch_queue_create.restype = ctypes.c_void_p

    def _serial_capture_queue(monitor_id):
        pointer = _libdispatch.dispatch_queue_create(
            f"com.app.worker.{monitor_id}".encode("ascii"),
            None,
        )
        return objc.objc_object(c_void_p=pointer)

    class _ScreenCaptureOutput(NSObject):
        def initWithOwner_(self, owner):
            self = objc.super(_ScreenCaptureOutput, self).init()
            if self is not None:
                self.owner = owner
            return self

        def stream_didOutputSampleBuffer_ofType_(
            self, _stream, sample_buffer, output_type
        ):
            if output_type != SCStreamOutputTypeScreen:
                return
            try:
                self.owner.last_status_at = time.perf_counter()
                attachments = CMSampleBufferGetSampleAttachmentsArray(
                    sample_buffer, False
                )
                if attachments:
                    attachment = attachments[0]
                    if hasattr(attachment, "get"):
                        status = attachment.get(SCStreamFrameInfoStatus)
                    else:
                        status = attachment.objectForKey_(SCStreamFrameInfoStatus)
                    if status is not None:
                        status = int(status)
                        if status == SCFrameStatusIdle:
                            # Idle means the display has not changed, not that
                            # ScreenCaptureKit stopped delivering updates. Keep
                            # that state separate from a real producer stall so
                            # the recovery loop does not continuously tear down
                            # a healthy stream while the desktop is static.
                            self.owner.is_idle = True
                            self.owner.non_idle_statuses = 0
                            return
                        if status != SCFrameStatusComplete:
                            self.owner.is_idle = False
                            self.owner.non_idle_statuses += 1
                            return
                pixel_buffer = CMSampleBufferGetImageBuffer(sample_buffer)
                if pixel_buffer is None:
                    return
                Quartz.CVPixelBufferLockBaseAddress(
                    pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly
                )
                try:
                    width = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
                    height = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))
                    row_bytes = int(Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer))
                    data_size = int(Quartz.CVPixelBufferGetDataSize(pixel_buffer))
                    address = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
                    memory = address.as_buffer(data_size)
                    padded = np.ndarray(
                        (height, row_bytes // 4, 4),
                        dtype=np.uint8,
                        buffer=memory,
                    )
                    self.owner.is_idle = False
                    self.owner.non_idle_statuses = 0
                    self.owner.store.publish(padded[:, :width, :])
                finally:
                    Quartz.CVPixelBufferUnlockBaseAddress(
                        pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly
                    )
            except Exception:
                logger.exception("ScreenCaptureKit frame callback failed")

    class _ScreenCaptureDelegate(NSObject):
        def initWithOwner_(self, owner):
            self = objc.super(_ScreenCaptureDelegate, self).init()
            if self is not None:
                self.owner = owner
            return self

        def stream_didStopWithError_(self, _stream, error):
            self.owner.capture_stopped(error)

    class _ScreenCaptureKitStream:
        def __init__(self, display, monitor: MonitorGeometry, fps: int):
            self.display = display
            self.monitor = monitor
            self.fps = fps
            self.store = _LatestFrameStore(monitor)
            self.output = None
            self.delegate = None
            self.stream = None
            self.sample_queue = None
            self.is_idle = False
            self.non_idle_statuses = 0
            self.last_status_at = time.perf_counter()
            self.interrupted = threading.Event()
            self.stop_requested = False
            self.last_error = None

        def capture_stopped(self, error):
            if self.stop_requested:
                return
            self.last_error = error
            self.is_idle = False
            self.last_status_at = time.perf_counter()
            self.interrupted.set()
            logger.warning("ScreenCaptureKit was interrupted: %s", error)

        def start(self):
            self.interrupted.clear()
            self.stop_requested = False
            self.last_error = None
            content_filter = SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                self.display, []
            )
            configuration = SCStreamConfiguration.alloc().init()
            configuration.setWidth_(self.monitor.width)
            configuration.setHeight_(self.monitor.height)
            configuration.setPixelFormat_(Quartz.kCVPixelFormatType_32BGRA)
            configuration.setMinimumFrameInterval_(CMTimeMake(1, self.fps))
            # Two buffers absorb a short WindowServer scheduling hiccup without
            # retaining the extra 33 ms of latency caused by a three-frame queue.
            configuration.setQueueDepth_(2)
            configuration.setShowsCursor_(True)
            configuration.setCapturesAudio_(False)
            if configuration.respondsToSelector_("setShouldBeOpaque:"):
                configuration.setShouldBeOpaque_(True)

            self.output = _ScreenCaptureOutput.alloc().initWithOwner_(self)
            self.delegate = _ScreenCaptureDelegate.alloc().initWithOwner_(self)
            self.sample_queue = _serial_capture_queue(self.monitor.id)
            self.stream = SCStream.alloc().initWithFilter_configuration_delegate_(
                content_filter, configuration, self.delegate
            )
            added, error = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
                self.output,
                SCStreamOutputTypeScreen,
                self.sample_queue,
                None,
            )
            if not added:
                raise RuntimeError(f"Could not add ScreenCaptureKit output: {error}")

            start_error = []
            started = threading.Event()

            def completion(error):
                if error:
                    start_error.append(error)
                started.set()

            self.stream.startCaptureWithCompletionHandler_(completion)
            # Some macOS releases deliver frames before the completion callback.
            snapshot = self.store.wait(timeout=5)
            if snapshot is None and not started.wait(1):
                raise RuntimeError("ScreenCaptureKit did not deliver its first frame")
            if start_error:
                raise RuntimeError(f"Could not start ScreenCaptureKit: {start_error[0]}")
            logger.info(
                "ScreenCaptureKit started for monitor %d at %d FPS.",
                self.monitor.id,
                self.fps,
            )

        def stop(self):
            if self.stream is None:
                return
            self.stop_requested = True
            if self.interrupted.is_set():
                self.stream = None
                self.output = None
                self.delegate = None
                self.sample_queue = None
                return
            stopped = threading.Event()

            def completion(error):
                if error:
                    logger.warning("ScreenCaptureKit stop error: %s", error)
                stopped.set()

            self.stream.stopCaptureWithCompletionHandler_(completion)
            stopped.wait(2)
            self.stream = None
            self.output = None
            self.delegate = None
            self.sample_queue = None


class SharedCaptureHub:
    """One continuous capture stream per monitor, shared across every client."""

    # ScreenCaptureKit can occasionally pause for a little over a second while
    # macOS switches spaces, wakes a display, or changes window composition.
    # Restarting on the first missed frame makes that harmless pause much worse
    # by tearing down a healthy capture session. Only recover after a sustained
    # sequence of missed waits.
    STALL_MISSES_BEFORE_RESTART = 3
    STALL_SECONDS_BEFORE_RESTART = 3.0

    def __init__(self, capturer, fps: int = 30):
        self.capturer = capturer
        self.fps = fps
        # Shared RTP timestamp origin for all tracks.  Keeping it on the hub
        # means viewers joining at different times still see the same frame
        # timestamps, which is required for the shared encoder cache.
        self.media_epoch = time.perf_counter()
        self.lock = threading.Lock()
        self.restart_lock = threading.Lock()
        self.streams = {}
        self.screen_capture_kit = False
        self._sc_displays = {}
        # When stealth capture is enabled (the default), use the least
        # detectable backend on each platform:
        #   macOS  – skip ScreenCaptureKit (shows recording indicator);
        #            MSS uses CGWindowListCreateImage which is invisible.
        #   Windows – skip DXcam/DXGI Desktop Duplication (detectable by
        #            proctoring software); MSS uses GDI BitBlt instead.
        stealth = os.environ.get("PROJECTX_STEALTH_CAPTURE", "1").strip()
        self._stealth = stealth not in ("0", "false", "no", "off")
        if sys.platform == "darwin" and not self._stealth:
            try:
                self._load_screen_capture_kit_displays()
                self.screen_capture_kit = bool(self._sc_displays)
            except Exception as error:
                logger.warning("ScreenCaptureKit unavailable, using MSS: %s", error)
        elif self._stealth:
            logger.info(
                "Stealth capture enabled; using MSS backend (no recording indicator)."
            )

    def _load_screen_capture_kit_displays(self):
        completed = threading.Event()
        result = {}

        def completion(content, error):
            result["content"] = content
            result["error"] = error
            completed.set()

        SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            False, True, completion
        )
        if not completed.wait(5):
            raise RuntimeError("Timed out enumerating ScreenCaptureKit displays")
        if result.get("error"):
            raise RuntimeError(result["error"])
        displays = result["content"].displays()
        for monitor, display in zip(self.capturer.monitors(refresh=True), displays):
            self._sc_displays[monitor.id] = display

    def _stream(self, monitor_id: int):
        with self.lock:
            stream = self.streams.get(monitor_id)
            if stream is not None:
                return stream
            monitor = self.capturer.monitor(monitor_id)
            if self.screen_capture_kit and monitor_id in self._sc_displays:
                stream = _ScreenCaptureKitStream(
                    self._sc_displays[monitor_id], monitor, self.fps
                )
            elif sys.platform == "win32" and DXCAM_AVAILABLE and not self._stealth:
                stream = _DXCamPollingStream(monitor, self.fps)
            else:
                stream = _MSSPollingStream(self.capturer, monitor, self.fps)
            try:
                stream.start()
            except Exception as error:
                if not isinstance(stream, _DXCamPollingStream):
                    raise
                logger.warning("DXcam unavailable, using MSS capture: %s", error)
                stream = _MSSPollingStream(self.capturer, monitor, self.fps)
                stream.start()
            self.streams[monitor_id] = stream
            return stream

    def _restart_stalled_stream(self, monitor_id: int, stalled_stream):
        """Replace a capture producer that stopped delivering frames."""
        with self.restart_lock:
            with self.lock:
                current = self.streams.get(monitor_id)
                if current is not stalled_stream:
                    replacement = current
                else:
                    replacement = None
                    self.streams.pop(monitor_id, None)
            if current is not stalled_stream:
                return replacement or self._stream(monitor_id)

            stale_for = max(
                0.0,
                time.perf_counter() - stalled_stream.store.captured_at,
            )
            logger.warning(
                "Capture stream %d stalled for %.1f seconds; restarting it.",
                monitor_id,
                stale_for,
            )
            stalled_stream.stop()
            return self._stream(monitor_id)

    def _wait_with_recovery(
        self,
        monitor_id: int,
        after_sequence: int,
        timeout: float,
    ):
        stream = self._stream(monitor_id)
        interrupted = getattr(stream, "interrupted", None)
        if interrupted is not None and interrupted.is_set():
            stream = self._restart_stalled_stream(monitor_id, stream)
            if stream is None:
                return None, None
            return stream, stream.store.wait(0, max(2.0, timeout))
        # Sequence numbers restart with a recovered producer. A consumer still
        # holding the old sequence must accept the first frame immediately.
        if after_sequence > stream.store.sequence:
            after_sequence = 0
        snapshot = None
        consecutive_misses = 0
        for _attempt in range(self.STALL_MISSES_BEFORE_RESTART):
            snapshot = stream.store.wait(after_sequence, timeout)
            if snapshot is not None:
                return stream, snapshot
            consecutive_misses += 1

        captured_at = stream.store.captured_at
        stale_for = (
            time.perf_counter() - captured_at if captured_at else float("inf")
        )
        stall_threshold = max(
            self.STALL_SECONDS_BEFORE_RESTART,
            timeout * self.STALL_MISSES_BEFORE_RESTART,
        )
        # Any recent ScreenCaptureKit status callback proves that the producer
        # is alive. In particular, macOS emits non-complete/blank statuses for
        # protected content; repeatedly tearing down that healthy stream makes
        # the rest of the desktop unavailable too. Genuine stream termination
        # is handled immediately by the SCStream delegate above.
        if (
            time.perf_counter()
            - getattr(stream, "last_status_at", float("-inf"))
            < stall_threshold
        ):
            return stream, None
        if (
            consecutive_misses < self.STALL_MISSES_BEFORE_RESTART
            or stale_for < stall_threshold
        ):
            return stream, None

        stream = self._restart_stalled_stream(monitor_id, stream)
        if stream is None:
            return None, None
        return stream, stream.store.wait(0, max(2.0, timeout))

    def wait_for_frame(
        self, monitor_id: int, after_sequence: int = 0, timeout: float = 1.0
    ) -> FrameSnapshot | None:
        _stream, snapshot = self._wait_with_recovery(
            monitor_id, after_sequence, timeout
        )
        return snapshot

    def jpeg(
        self,
        monitor_id: int,
        after_sequence: int,
        quality: int,
        scale: float,
        timeout: float = 1.0,
    ) -> tuple[bytes | None, int]:
        stream, snapshot = self._wait_with_recovery(
            monitor_id, after_sequence, timeout
        )
        if snapshot is None:
            return None, after_sequence
        return stream.store.jpeg(snapshot, quality, scale), snapshot.sequence

    def stop(self):
        with self.lock:
            streams = list(self.streams.values())
            self.streams.clear()
        for stream in streams:
            stream.stop()
        if streams:
            logger.info("Released %d shared screen capture stream(s).", len(streams))

    def metrics(self):
        with self.lock:
            streams = dict(self.streams)
        return {
            str(monitor_id): stream.store.metrics()
            for monitor_id, stream in streams.items()
        }
