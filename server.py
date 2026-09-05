import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
import queue
import random
import secrets
import struct
import sys
import tempfile
import threading
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import time
import argparse
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType
import aiofiles
import sounddevice as sd

# MSS must load before PyAutoGUI on Windows to avoid process-DPI side effects.
from capture_backends import create_screen_capture
from camera_process import CameraCaptureProcess
from shared_capture import SharedCaptureHub
from webrtc_media import VideoToolboxH264Encoder, WebRTCManager

import pyperclip
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Controller as MouseController, Button

if sys.platform == "darwin":
    import macos_input
else:
    macos_input = None

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("ProjectX")

_host_lock_handle = None


def configured_ice_servers(environ=None):
    """Return optional authenticated TURN configuration for viewer ICE."""
    environ = environ or os.environ
    urls = [
        value.strip()
        for value in environ.get("PROJECTX_TURN_URLS", "").split(",")
        if value.strip()
    ]
    if not urls:
        return []
    server = {"urls": urls}
    username = environ.get("PROJECTX_TURN_USERNAME", "")
    credential = environ.get("PROJECTX_TURN_CREDENTIAL", "")
    if username:
        server["username"] = username
    if credential:
        server["credential"] = credential
    return [server]


def acquire_host_lock():
    """Prevent concurrent capture servers, which can stall CoreGraphics on macOS."""
    global _host_lock_handle
    lock_path = os.path.join(tempfile.gettempdir(), "webremote-host.lock")
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as error:
        handle.close()
        raise RuntimeError(
            "Another Project X host is already running. Stop it before starting a new one."
        ) from error

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _host_lock_handle = handle


def release_host_lock():
    """Release the process-wide host lock after an in-process desktop stop."""
    global _host_lock_handle
    if _host_lock_handle is None:
        return
    try:
        _host_lock_handle.close()
    finally:
        _host_lock_handle = None

mouse = MouseController()
keyboard = KeyboardController()

# Browser KeyboardEvent.code -> pynput key name. Some keys are not available on
# every host platform, so they are resolved without touching missing attributes.
KEY_NAMES = {
    "Backspace": "backspace",
    "Tab": "tab",
    "Enter": "enter",
    "Shift": "shift",
    "ShiftLeft": "shift_l",
    "ShiftRight": "shift_r",
    "Control": "ctrl",
    "ControlLeft": "ctrl_l",
    "ControlRight": "ctrl_r",
    "Alt": "alt",
    "AltLeft": "alt_l",
    "AltRight": "alt_r",
    "Pause": "pause",
    "CapsLock": "caps_lock",
    "Escape": "esc",
    "Space": "space",
    "PageUp": "page_up",
    "PageDown": "page_down",
    "End": "end",
    "Home": "home",
    "ArrowLeft": "left",
    "ArrowUp": "up",
    "ArrowRight": "right",
    "ArrowDown": "down",
    "PrintScreen": "print_screen",
    "Insert": "insert",
    "Delete": "delete",
    "Meta": "cmd",
    "MetaLeft": "cmd_l",
    "MetaRight": "cmd_r",
    "ContextMenu": "menu",
    **{f"F{number}": f"f{number}" for number in range(1, 13)},
}
KEY_MAP = {code: getattr(Key, name, None) for code, name in KEY_NAMES.items()}


class MicrophoneCapture:
    """Low-latency mono PCM microphone capture with a bounded latest-data queue."""

    def __init__(self, sample_rate=16000, block_duration=0.04):
        self.requested_sample_rate = sample_rate
        self.block_duration = block_duration
        self.sample_rate = sample_rate
        self.stream = None
        self.is_running = False
        self._lock = threading.Lock()
        self._chunks = queue.Queue(maxsize=3)

    def _callback(self, indata, _frames, _time_info, status):
        if status:
            logger.debug("Microphone status: %s", status)
        chunk = bytes(indata)
        if self._chunks.full():
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                pass
        try:
            self._chunks.put_nowait(chunk)
        except queue.Full:
            pass

    def start(self):
        with self._lock:
            if self.is_running:
                return True
            while not self._chunks.empty():
                try:
                    self._chunks.get_nowait()
                except queue.Empty:
                    break
            try:
                sample_rate = self.requested_sample_rate
                try:
                    sd.check_input_settings(
                        samplerate=sample_rate,
                        channels=1,
                        dtype="int16",
                    )
                except Exception:
                    device = sd.query_devices(kind="input")
                    sample_rate = int(device["default_samplerate"])

                blocksize = max(160, round(sample_rate * self.block_duration))
                self.stream = sd.RawInputStream(
                    samplerate=sample_rate,
                    blocksize=blocksize,
                    channels=1,
                    dtype="int16",
                    latency="low",
                    callback=self._callback,
                )
                self.stream.start()
                self.sample_rate = int(sample_rate)
                self.is_running = True
                logger.info(
                    "Microphone started at %d Hz mono, %d-sample blocks.",
                    self.sample_rate,
                    blocksize,
                )
                return True
            except Exception as error:
                logger.error("Could not start microphone: %s", error)
                if self.stream:
                    try:
                        self.stream.close()
                    except Exception:
                        pass
                self.stream = None
                return False

    def stop(self):
        with self._lock:
            was_running = self.is_running or self.stream is not None
            self.is_running = False
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            if was_running:
                logger.info("Microphone released.")

    def read_chunk(self, timeout=0.2):
        if not self.is_running:
            return None
        try:
            return self._chunks.get(timeout=timeout)
        except queue.Empty:
            return None


@dataclass(slots=True)
class ClientSession:
    monitor_id: int
    remote_address: str | None = None
    authenticated: bool = False
    quality: int = 55
    scale: float = 0.85
    target_fps: int = 30
    requested_fps: int = 30
    upload_token: str | None = None
    auth_failures: int = 0
    use_webrtc: bool = False
    requested_scale: float = 0.85
    webrtc_scale: float = 0.85
    good_media_reports: int = 0
    bad_media_reports: int = 0
    media_report_count: int = 0
    bitrate_bad_reports: int = 0
    last_media_fps: float = 0.0
    last_media_jitter_ms: float = 0.0
    last_media_loss_percent: float = 0.0
    last_route_rtt_ms: float | None = None
    best_route_rtt_ms: float | None = None
    last_control_rtt_ms: float | None = None
    best_control_rtt_ms: float | None = None
    last_route_type: str = "unknown"
    input_count: int = 0
    input_processing_total_ms: float = 0.0
    input_processing_max_ms: float = 0.0
    control_ws: object | None = None
    media_ws: object | None = None
    camera_enabled: bool = False
    microphone_enabled: bool = False
    frame_ack_event: object = field(default_factory=asyncio.Event)
    scroll_remainder: float = 0.0
    pending_mouse_move: dict | None = None
    mouse_move_task: object | None = None
    mouse_lock: object = field(default_factory=asyncio.Lock)
    pressed_keys: set = field(default_factory=set)
    pressed_buttons: set = field(default_factory=set)
    typing_task: object | None = None
    typing_cancel: object = field(default_factory=threading.Event)
    typing_lock: object = field(default_factory=asyncio.Lock)


class RemoteServer:
    # Keep at most a small amount of encoded media in the asyncio transport.
    # Waiting for the browser's render ACK here couples a video frame to a
    # full network round-trip (an 800 ms path becomes roughly 1 FPS).  The
    # writer already applies flow control; this cap prevents a slow link from
    # accumulating a stale multi-second queue while allowing frames to flow at
    # the requested cadence on a healthy LAN.
    MEDIA_WRITE_BUFFER_LIMIT = 512 * 1024
    # Limit application-level JPEG work as well as the socket's byte buffer.
    # The viewer acknowledges rendered or deliberately dropped frames, keeping
    # network and decoder queues short without reducing healthy-LAN frame rate.
    MAX_JPEG_FRAMES_IN_FLIGHT = 4
    SCROLL_PIXELS_PER_STEP = 100.0
    MAX_SCROLL_STEPS_PER_EVENT = 3

    def __init__(self, pin="1234", port=5000):
        self.pin = str(pin)
        self.port = port
        self.capturer = create_screen_capture(include_cursor=True)
        self.capture_hub = SharedCaptureHub(self.capturer, fps=30)
        self.webrtc = WebRTCManager(
            self.capture_hub,
            input_handler=self._handle_realtime_channel_input,
        )
        self.camera = CameraCaptureProcess(0)
        self.camera_subscribers = set()
        self.camera_task = None
        self.camera_lifecycle_lock = asyncio.Lock()
        self.microphone = MicrophoneCapture()
        self.microphone_subscribers = set()
        self.microphone_task = None
        self.microphone_lifecycle_lock = asyncio.Lock()
        self.clients = {}
        self.upload_tokens = set()
        self.token_sessions = {}

    def capture_frame(self, session, after_sequence=0):
        """Capture and compress a frame using the client's selected settings."""
        try:
            return self.capture_hub.jpeg(
                monitor_id=session.monitor_id,
                after_sequence=after_sequence,
                quality=session.quality,
                scale=session.scale,
            )
        except Exception as error:
            logger.error("Screen capture error: %s", error)
            return None, after_sequence

    def _move_mouse(self, data, session):
        monitor = self.capturer.monitor(session.monitor_id)
        normalized_x = max(0.0, min(1.0, float(data.get("x", 0))))
        normalized_y = max(0.0, min(1.0, float(data.get("y", 0))))
        absolute_x = round(monitor.left + normalized_x * max(0, monitor.width - 1))
        absolute_y = round(monitor.top + normalized_y * max(0, monitor.height - 1))
        if macos_input is not None:
            macos_input.move(absolute_x, absolute_y)
        else:
            mouse.position = (absolute_x, absolute_y)

    async def _mouse_move_worker(self, session):
        """Apply only the newest pointer position without blocking the event loop."""
        try:
            while session.pending_mouse_move is not None:
                data = session.pending_mouse_move
                session.pending_mouse_move = None
                async with session.mouse_lock:
                    await asyncio.to_thread(self._move_mouse, data, session)
        finally:
            session.mouse_move_task = None

    async def _handle_realtime_channel_input(self, data, session, channel):
        """Process latency-sensitive input carried beside WebRTC video."""
        if not session.authenticated:
            return
        allowed = {
            "projectx-pointer": {"mouse_move"},
            "projectx-control": {
                "mouse_down",
                "mouse_up",
                "mouse_click",
                "mouse_dblclick",
                "mouse_wheel",
                "key_down",
                "key_up",
            },
        }
        if data.get("type") not in allowed.get(channel.label, set()):
            return
        started = time.perf_counter()
        await self.handle_input_message(data, session.control_ws, session)
        processing_ms = (time.perf_counter() - started) * 1000
        session.input_count += 1
        session.input_processing_total_ms += processing_ms
        session.input_processing_max_ms = max(
            session.input_processing_max_ms,
            processing_ms,
        )
        input_id = data.get("input_id")
        if input_id is not None and channel.readyState == "open":
            channel.send(json.dumps({
                "type": "input_ack",
                "input_id": input_id,
                "server_ms": round(processing_ms, 3),
            }))

    def performance_snapshot(self):
        sessions = []
        for session in self.clients.values():
            if not session.authenticated:
                continue
            sessions.append({
                "remote_address": session.remote_address,
                "webrtc": session.use_webrtc,
                "target_fps": session.target_fps,
                "requested_fps": session.requested_fps,
                "scale": round(session.webrtc_scale, 2),
                "media_fps": round(session.last_media_fps, 1),
                "media_jitter_ms": round(session.last_media_jitter_ms, 1),
                "media_loss_percent": round(
                    session.last_media_loss_percent, 2
                ),
                "route_rtt_ms": session.last_route_rtt_ms,
                "route_queue_delay_ms": round(
                    max(
                        0.0,
                        (session.last_route_rtt_ms or 0.0)
                        - (session.best_route_rtt_ms or 0.0),
                    ),
                    1,
                ),
                "route_type": session.last_route_type,
                "control_rtt_ms": session.last_control_rtt_ms,
                "input_count": session.input_count,
                "input_processing_avg_ms": round(
                    session.input_processing_total_ms / max(1, session.input_count),
                    3,
                ),
                "input_processing_max_ms": round(
                    session.input_processing_max_ms, 3
                ),
            })
        return {
            "capture": self.capture_hub.metrics(),
            "encoder": VideoToolboxH264Encoder.shared_pool_stats()
            if sys.platform == "darwin" else {},
            "sessions": sessions,
        }

    @staticmethod
    def _mouse_button(name):
        return {
            "left": Button.left,
            "right": Button.right,
            "middle": Button.middle,
        }.get(name, Button.left)

    @staticmethod
    def _keyboard_key(data):
        mapped = KEY_MAP.get(data.get("code"))
        if mapped is not None:
            return mapped
        value = data.get("key")
        if isinstance(value, str) and len(value) == 1:
            return value
        return None

    @staticmethod
    def _press_combo(keys):
        special = {
            "command": Key.cmd,
            "win": Key.cmd,
            "ctrl": Key.ctrl,
            "alt": Key.alt,
            "delete": Key.delete,
            "f3": Key.f3,
            "tab": Key.tab,
        }
        resolved = [special.get(value, value) for value in keys]
        try:
            for value in resolved:
                keyboard.press(value)
        finally:
            for value in reversed(resolved):
                try:
                    keyboard.release(value)
                except Exception:
                    pass

    @staticmethod
    def _type_text(
        controller,
        text,
        cancel_event,
        chars_per_second,
        clock=None,
        random_uniform=None,
        code_mode=False,
        platform_name=None,
    ):
        """Inject text near a target rate with mild human-like timing jitter."""
        clock = clock or time.perf_counter
        random_uniform = random_uniform or random.uniform
        platform_name = platform_name or sys.platform
        typed = 0
        skipped = 0
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if code_mode:
            # Native Tab is an editor command, not literal text. Expand it
            # using normal four-column tab stops so indentation is deterministic.
            normalized = normalized.expandtabs(4)
        special_keys = {"\n": Key.enter, "\t": Key.tab, "\b": Key.backspace}
        interval = 1.0 / chars_per_second
        next_key_time = clock()

        for index, character in enumerate(normalized):
            if cancel_event.is_set():
                break
            key_value = special_keys.get(character, character)
            try:
                controller.press(key_value)
                controller.release(key_value)
                typed += 1
            except Exception:
                skipped += 1

            if code_mode and character == "\n" and index < len(normalized) - 1:
                # Editors such as VS Code auto-indent after Enter. Return to
                # column zero before typing the source line's own indentation.
                try:
                    if platform_name == "darwin":
                        controller.press(Key.cmd)
                        controller.press(Key.left)
                        controller.release(Key.left)
                        controller.release(Key.cmd)
                    else:
                        controller.press(Key.home)
                        controller.release(Key.home)
                except Exception:
                    skipped += 1

            if index < len(normalized) - 1:
                # Keep variation subtle: the requested rate remains the
                # long-run average while consecutive strokes feel less robotic.
                delay_factor = random_uniform(0.88, 1.12)
                if code_mode and character == "\n":
                    delay_factor *= 1.8
                elif code_mode and character in "{}[]();,:\"'<>+-=*/":
                    delay_factor *= 1.25
                next_key_time += interval * delay_factor
                wait_time = next_key_time - clock()
                if wait_time > 0 and cancel_event.wait(wait_time):
                    break
        return typed, skipped

    async def _run_text_injection(
        self, text, chars_per_second, code_mode, request_id, ws, session, cancel_event
    ):
        try:
            if not ws.closed:
                await ws.send_json({
                    "type": "typing_status",
                    "status": "started",
                    "request_id": request_id,
                    "total": len(text),
                    "chars_per_second": chars_per_second,
                    "code_mode": code_mode,
                })
            async with session.typing_lock:
                typed, skipped = await asyncio.to_thread(
                    self._type_text,
                    keyboard,
                    text,
                    cancel_event,
                    chars_per_second,
                    None,
                    None,
                    code_mode,
                )
            if not ws.closed:
                await ws.send_json({
                    "type": "typing_status",
                    "status": "cancelled" if cancel_event.is_set() else "complete",
                    "request_id": request_id,
                    "typed": typed,
                    "skipped": skipped,
                })
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        except Exception as error:
            logger.warning("Text injection failed: %s", error)
            if not ws.closed:
                await ws.send_json({
                    "type": "typing_status",
                    "status": "error",
                    "request_id": request_id,
                    "message": "The host could not inject that text.",
                })
        finally:
            if session.typing_task is asyncio.current_task():
                session.typing_task = None

    async def _set_camera_subscription(self, session, enabled, notify=True):
        session.camera_enabled = enabled
        media_ws = session.media_ws
        control_ws = session.control_ws
        async with self.camera_lifecycle_lock:
            if enabled:
                if media_ws is None or media_ws.closed:
                    if notify and control_ws and not control_ws.closed:
                        await control_ws.send_json(
                            {"type": "camera_status", "status": "starting"}
                        )
                    return
                self.camera_subscribers.add(media_ws)
                if not self.camera_task or self.camera_task.done():
                    if notify and control_ws and not control_ws.closed:
                        await control_ws.send_json(
                            {"type": "camera_status", "status": "starting"}
                        )
                    started = await asyncio.to_thread(self.camera.start)
                    if started:
                        self.camera_task = asyncio.create_task(self.camera_worker())
                    else:
                        self.camera_subscribers.discard(media_ws)
                status = "on" if self.camera.is_running else "error"
                if notify and control_ws and not control_ws.closed:
                    await control_ws.send_json(
                        {"type": "camera_status", "status": status}
                    )
            else:
                if media_ws is not None:
                    self.camera_subscribers.discard(media_ws)
                if not self.camera_subscribers and self.camera_task:
                    task = self.camera_task
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if notify and control_ws and not control_ws.closed:
                    await control_ws.send_json(
                        {"type": "camera_status", "status": "off"}
                    )

    async def _set_microphone_subscription(self, session, enabled, notify=True):
        session.microphone_enabled = enabled
        media_ws = session.media_ws
        control_ws = session.control_ws
        async with self.microphone_lifecycle_lock:
            if enabled:
                if media_ws is None or media_ws.closed:
                    if notify and control_ws and not control_ws.closed:
                        await control_ws.send_json(
                            {"type": "microphone_status", "status": "starting"}
                        )
                    return
                self.microphone_subscribers.add(media_ws)
                if not self.microphone_task or self.microphone_task.done():
                    if notify and control_ws and not control_ws.closed:
                        await control_ws.send_json(
                            {"type": "microphone_status", "status": "starting"}
                        )
                    started = await asyncio.to_thread(self.microphone.start)
                    if started:
                        self.microphone_task = asyncio.create_task(self.microphone_worker())
                    else:
                        self.microphone_subscribers.discard(media_ws)
                status = "on" if self.microphone.is_running else "error"
                if notify and control_ws and not control_ws.closed:
                    await control_ws.send_json(
                        {"type": "microphone_status", "status": status}
                    )
            else:
                if media_ws is not None:
                    self.microphone_subscribers.discard(media_ws)
                if not self.microphone_subscribers and self.microphone_task:
                    task = self.microphone_task
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if notify and control_ws and not control_ws.closed:
                    await control_ws.send_json(
                        {"type": "microphone_status", "status": "off"}
                    )

    async def handle_input_message(self, data, ws, session):
        """Process validated client mouse, keyboard, and control commands."""
        event_type = data.get("type")

        if event_type == "mouse_move":
            session.pending_mouse_move = {"x": data.get("x"), "y": data.get("y")}
            if session.mouse_move_task is None or session.mouse_move_task.done():
                session.mouse_move_task = asyncio.create_task(
                    self._mouse_move_worker(session)
                )

        elif event_type in {"mouse_down", "mouse_up", "mouse_click"}:
            async with session.mouse_lock:
                if "x" in data and "y" in data:
                    await asyncio.to_thread(self._move_mouse, data, session)
                button = self._mouse_button(data.get("button", "left"))
                button_name = data.get("button", "left")
                if macos_input is not None:
                    if event_type == "mouse_down":
                        macos_input.mouse_button(button_name, True)
                        session.pressed_buttons.add(("native", button_name))
                    elif event_type == "mouse_up":
                        macos_input.mouse_button(button_name, False)
                        session.pressed_buttons.discard(("native", button_name))
                    else:
                        macos_input.click(button_name)
                    return
                if event_type == "mouse_down":
                    await asyncio.to_thread(mouse.press, button)
                    session.pressed_buttons.add(button)
                elif event_type == "mouse_up":
                    await asyncio.to_thread(mouse.release, button)
                    session.pressed_buttons.discard(button)
                else:
                    await asyncio.to_thread(mouse.click, button, 1)

        elif event_type == "mouse_dblclick":
            async with session.mouse_lock:
                if "x" in data and "y" in data:
                    await asyncio.to_thread(self._move_mouse, data, session)
                if macos_input is not None:
                    macos_input.click("left", 2)
                else:
                    await asyncio.to_thread(mouse.click, Button.left, 2)

        elif event_type == "mouse_wheel":
            delta_y = float(data.get("deltaY", 0))
            if macos_input is not None:
                macos_input.scroll(delta_y)
            else:
                # Precision trackpads emit many small pixel deltas. Converting
                # every event into two full Windows wheel notches makes a tiny
                # Mac gesture jump several pages. Accumulate those pixels into
                # discrete notches and cap unusually large browser bursts.
                delta_mode = int(data.get("deltaMode", 0))
                pixel_multiplier = {0: 1.0, 1: 16.0, 2: 800.0}.get(
                    delta_mode, 1.0
                )
                pixel_delta = max(
                    -self.SCROLL_PIXELS_PER_STEP * self.MAX_SCROLL_STEPS_PER_EVENT,
                    min(
                        self.SCROLL_PIXELS_PER_STEP
                        * self.MAX_SCROLL_STEPS_PER_EVENT,
                        delta_y * pixel_multiplier,
                    ),
                )
                accumulated = session.scroll_remainder + (
                    pixel_delta / self.SCROLL_PIXELS_PER_STEP
                )
                accumulated = max(
                    -float(self.MAX_SCROLL_STEPS_PER_EVENT),
                    min(float(self.MAX_SCROLL_STEPS_PER_EVENT), accumulated),
                )
                steps = int(accumulated)
                session.scroll_remainder = accumulated - steps
                if steps:
                    await asyncio.to_thread(mouse.scroll, 0, -steps)

        elif event_type in {"key_down", "key_up"}:
            if macos_input is not None and macos_input.key(
                str(data.get("code", "")),
                event_type == "key_down",
            ):
                native_key = ("native", str(data.get("code", "")))
                if event_type == "key_down":
                    session.pressed_keys.add(native_key)
                else:
                    session.pressed_keys.discard(native_key)
                return
            key_value = self._keyboard_key(data)
            if key_value is not None:
                if event_type == "key_down":
                    await asyncio.to_thread(keyboard.press, key_value)
                    session.pressed_keys.add(key_value)
                else:
                    await asyncio.to_thread(keyboard.release, key_value)
                    session.pressed_keys.discard(key_value)

        elif event_type == "type_text":
            text = data.get("text")
            if not isinstance(text, str):
                raise TypeError("Text must be a string")
            if not text:
                raise ValueError("Enter some text first")
            if len(text) > 10000:
                raise ValueError("Text is limited to 10,000 characters")
            chars_per_second = int(data.get("chars_per_second", 300))
            if not 1 <= chars_per_second <= 1000:
                raise ValueError("Typing speed must be between 1 and 1,000 chars/sec")
            code_mode = data.get("code_mode", False)
            if not isinstance(code_mode, bool):
                raise TypeError("Code-Safe Mode must be true or false")
            if code_mode and chars_per_second > 120:
                raise ValueError("Code-Safe Mode is limited to 120 chars/sec")

            session.typing_cancel.set()
            request_id = str(data.get("request_id", ""))[:64]
            cancel_event = threading.Event()
            session.typing_cancel = cancel_event
            session.typing_task = asyncio.create_task(
                self._run_text_injection(
                    text,
                    chars_per_second,
                    code_mode,
                    request_id,
                    ws,
                    session,
                    cancel_event,
                )
            )

        elif event_type == "cancel_typing":
            session.typing_cancel.set()

        elif event_type == "set_quality":
            session.quality = max(15, min(95, int(data.get("quality", 55))))
            session.scale = max(0.3, min(1.0, float(data.get("scale", 0.85))))
            session.requested_scale = session.scale
            # An explicit user preset immediately resets adaptive scaling.
            session.webrtc_scale = session.requested_scale
            session.requested_fps = max(5, min(30, int(data.get("fps", 30))))
            session.target_fps = session.requested_fps

        elif event_type == "media_stats":
            packet_loss = max(0.0, float(data.get("packetLoss", 0)))
            jitter = max(0.0, float(data.get("jitter", 0)))
            rendered_fps = max(0.0, float(data.get("fps", 0)))
            session.last_media_fps = rendered_fps
            session.last_media_jitter_ms = jitter * 1000
            session.last_media_loss_percent = packet_loss * 100
            route_rtt = data.get("routeRtt")
            route_type = str(data.get("routeType", "unknown"))[:16]
            route_queue_delay_ms = 0.0
            if route_rtt is not None:
                route_rtt_ms = round(max(0.0, float(route_rtt)) * 1000, 1)
                if route_type != session.last_route_type:
                    session.best_route_rtt_ms = route_rtt_ms
                elif (
                    session.best_route_rtt_ms is None
                    or route_rtt_ms < session.best_route_rtt_ms
                ):
                    session.best_route_rtt_ms = route_rtt_ms
                session.last_route_rtt_ms = route_rtt_ms
                route_queue_delay_ms = max(
                    0.0, route_rtt_ms - (session.best_route_rtt_ms or route_rtt_ms)
                )
            session.last_route_type = route_type
            control_queue_delay_ms = 0.0
            control_rtt = data.get("controlRttMs")
            if control_rtt is not None:
                control_rtt_ms = max(0.0, float(control_rtt))
                if (
                    session.best_control_rtt_ms is None
                    or control_rtt_ms < session.best_control_rtt_ms
                ):
                    session.best_control_rtt_ms = control_rtt_ms
                session.last_control_rtt_ms = round(control_rtt_ms, 1)
                control_queue_delay_ms = max(
                    0.0,
                    control_rtt_ms
                    - (session.best_control_rtt_ms or control_rtt_ms),
                )
            queue_delay_ms = max(route_queue_delay_ms, control_queue_delay_ms)
            session.media_report_count += 1
            target_fps = max(5, min(30, int(session.target_fps)))
            degraded = (
                packet_loss > 0.05
                or jitter > 0.04
                or (rendered_fps > 0 and rendered_fps < target_fps * 0.73)
                or queue_delay_ms > 120
            )
            healthy = (
                packet_loss < 0.01
                and jitter < 0.03
                and rendered_fps >= target_fps * 0.90
                and queue_delay_ms < 50
            )
            if degraded:
                session.bitrate_bad_reports += 1
                session.bad_media_reports += 1
                session.good_media_reports = 0
                if session.bad_media_reports >= 2:
                    session.webrtc_scale = max(0.55, session.webrtc_scale - 0.1)
                    session.target_fps = min(session.target_fps, 15)
                    session.bad_media_reports = 0
            elif healthy:
                session.bitrate_bad_reports = 0
                session.good_media_reports += 1
                session.bad_media_reports = 0
                if session.good_media_reports >= 8:
                    session.webrtc_scale = min(
                        session.requested_scale, session.webrtc_scale + 0.05
                    )
                    if session.target_fps < session.requested_fps:
                        session.target_fps = min(
                            session.requested_fps, session.target_fps + 5
                        )
                    session.good_media_reports = 0
            else:
                session.bitrate_bad_reports = 0
                session.good_media_reports = 0
                session.bad_media_reports = 0
            self.webrtc.set_session_media_health(
                session,
                session.bitrate_bad_reports >= 2,
            )
            if session.media_report_count % 3 == 0:
                logger.info(
                    "Client media: %.1f FPS, %.1f ms jitter, %.2f%% loss, "
                    "%.1f ms control RTT, scale %.2f, target %d FPS",
                    rendered_fps,
                    jitter * 1000,
                    packet_loss * 100,
                    session.last_control_rtt_ms or 0.0,
                    session.webrtc_scale,
                    session.target_fps,
                )
            await ws.send_json({
                "type": "adaptive_quality",
                "scale": round(session.webrtc_scale, 2),
                "fps": session.target_fps,
            })

        elif event_type == "media_transport":
            session.use_webrtc = data.get("transport") == "webrtc"

        elif event_type == "set_monitor":
            monitor_id = int(data.get("monitor", session.monitor_id))
            available_ids = {monitor.id for monitor in self.capturer.monitors(refresh=True)}
            if monitor_id in available_ids:
                session.monitor_id = monitor_id
                monitor = self.capturer.monitor(monitor_id)
                await ws.send_json({
                    "type": "monitor_changed",
                    "monitor": monitor.public_dict(),
                })

        elif event_type == "toggle_camera":
            await self._set_camera_subscription(
                session, bool(data.get("enabled", False))
            )

        elif event_type == "toggle_microphone":
            await self._set_microphone_subscription(
                session, bool(data.get("enabled", False))
            )

        elif event_type == "set_clipboard":
            text = data.get("text", "")
            if isinstance(text, str) and text:
                await asyncio.to_thread(pyperclip.copy, text)

        elif event_type == "get_clipboard":
            text = await asyncio.to_thread(pyperclip.paste)
            await ws.send_json({"type": "clipboard", "text": text})

        elif event_type == "special_combo":
            combo = data.get("combo")
            if sys.platform == "darwin":
                combos = {
                    "win_d": ("command", "f3"),
                    "alt_tab": ("command", "tab"),
                    "ctrl_c": ("command", "c"),
                    "ctrl_v": ("command", "v"),
                }
            else:
                combos = {
                    "ctrl_alt_del": ("ctrl", "alt", "delete"),
                    "win_d": ("win", "d"),
                    "alt_tab": ("alt", "tab"),
                    "win_r": ("win", "r"),
                    "ctrl_c": ("ctrl", "c"),
                    "ctrl_v": ("ctrl", "v"),
                }
            keys = combos.get(combo)
            if keys:
                await asyncio.to_thread(self._press_combo, keys)

    async def camera_worker(self):
        """Capture one camera frame and broadcast it to all subscribers."""
        try:
            while True:
                started = time.perf_counter()
                frame = await asyncio.to_thread(self.camera.grab_frame)
                subscribers = [ws for ws in self.camera_subscribers if not ws.closed]
                if frame and subscribers:
                    payload = b'\x02' + frame
                    results = await asyncio.gather(
                        *(
                            asyncio.wait_for(ws.send_bytes(payload), timeout=0.5)
                            for ws in subscribers
                        ),
                        return_exceptions=True,
                    )
                    for ws, result in zip(subscribers, results):
                        if isinstance(result, Exception):
                            asyncio.create_task(ws.close())
                elapsed = time.perf_counter() - started
                frame_interval = 1.0 / self.camera.fps
                await asyncio.sleep(max(0.002, frame_interval - elapsed))
        finally:
            await asyncio.to_thread(self.camera.stop)
            if self.camera_task is asyncio.current_task():
                self.camera_task = None

    async def microphone_worker(self):
        """Broadcast bounded PCM microphone chunks without delaying screen frames."""
        try:
            while True:
                chunk = await asyncio.to_thread(self.microphone.read_chunk)
                subscribers = [
                    ws for ws in self.microphone_subscribers if not ws.closed
                ]
                if chunk and subscribers:
                    # Tag 0x03, one padding byte, uint32 little-endian sample rate,
                    # followed by signed 16-bit little-endian mono PCM.
                    payload = (
                        b'\x03\x00'
                        + struct.pack("<I", self.microphone.sample_rate)
                        + chunk
                    )
                    results = await asyncio.gather(
                        *(
                            asyncio.wait_for(ws.send_bytes(payload), timeout=0.5)
                            for ws in subscribers
                        ),
                        return_exceptions=True,
                    )
                    for ws, result in zip(subscribers, results):
                        if isinstance(result, Exception):
                            asyncio.create_task(ws.close())
        finally:
            await asyncio.to_thread(self.microphone.stop)
            if self.microphone_task is asyncio.current_task():
                self.microphone_task = None

    async def stream_worker(self, ws, ack_event, session):
        """Send latest JPEG frames with bounded end-to-end back-pressure.

        A small frame window keeps a healthy LAN running at its requested FPS
        while preventing slow networks or decoders from accumulating seconds
        of stale screen updates. The socket byte cap remains a second guard.
        """
        sequence = 0
        frames_in_flight = 0
        loop = asyncio.get_running_loop()
        next_send_at = loop.time()
        while not ws.closed:
            if ack_event.is_set():
                ack_event.clear()
                frames_in_flight = max(0, frames_in_flight - 1)

            if session.use_webrtc:
                await asyncio.sleep(0.2)
                next_send_at = loop.time()
                frames_in_flight = 0
                continue

            interval = 1.0 / max(5, min(30, int(session.target_fps)))
            now = loop.time()
            delay = next_send_at - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = loop.time()
            # If capture/transport work took longer than one interval, do not
            # replay missed slots and create a burst of stale frames.
            if next_send_at < now - interval:
                next_send_at = now
            next_send_at += interval

            if frames_in_flight >= self.MAX_JPEG_FRAMES_IN_FLIGHT:
                try:
                    await asyncio.wait_for(ack_event.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                next_send_at = loop.time()
                continue

            writer = getattr(ws, "_writer", None)
            transport = getattr(writer, "transport", None)
            try:
                buffered = (
                    (transport.get_write_buffer_size() if transport else 0)
                    + int(getattr(writer, "_output_size", 0) or 0)
                )
            except (AttributeError, OSError):
                buffered = 0
            if buffered > self.MEDIA_WRITE_BUFFER_LIMIT:
                # Let the kernel drain instead of encoding a frame that would
                # sit behind old data.  Reset the schedule after the pause so
                # the next frame is the newest one available.
                await asyncio.sleep(min(interval, 0.01))
                next_send_at = loop.time()
                continue

            frame, sequence = await asyncio.to_thread(
                self.capture_frame, session, sequence
            )
            if frame and not ws.closed:
                try:
                    await ws.send_bytes(b'\x01' + frame)
                    frames_in_flight += 1
                except Exception:
                    break

    @staticmethod
    def _release_inputs(session):
        for button in tuple(session.pressed_buttons):
            try:
                if (
                    macos_input is not None
                    and isinstance(button, tuple)
                    and button[0] == "native"
                ):
                    macos_input.mouse_button(button[1], False)
                else:
                    mouse.release(button)
            except Exception:
                pass
        for key_value in tuple(session.pressed_keys):
            try:
                if (
                    macos_input is not None
                    and isinstance(key_value, tuple)
                    and key_value[0] == "native"
                ):
                    macos_input.key(key_value[1], False)
                else:
                    keyboard.release(key_value)
            except Exception:
                pass
        session.pressed_buttons.clear()
        session.pressed_keys.clear()

    async def ws_handler(self, request):
        """Latency-sensitive authentication, input, and control channel."""
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text="WebSocket origin does not match the host")
        ws = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024, heartbeat=30)
        await ws.prepare(request)

        primary_id = self.capturer.primary_monitor_id()
        session = ClientSession(
            monitor_id=primary_id,
            remote_address=request.remote,
        )
        session.control_ws = ws
        self.clients[ws] = session

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if not isinstance(data, dict):
                            continue
                    except (json.JSONDecodeError, TypeError):
                        continue

                    msg_type = data.get("type")
                    if msg_type == "auth":
                        client_pin = str(data.get("pin", ""))
                        if client_pin == self.pin or not self.pin:
                            session.authenticated = True
                            session.auth_failures = 0
                            if session.upload_token is None:
                                session.upload_token = secrets.token_urlsafe(32)
                                self.upload_tokens.add(session.upload_token)
                                self.token_sessions[session.upload_token] = session
                            monitors = self.capturer.monitors(refresh=True)
                            if session.monitor_id not in {m.id for m in monitors}:
                                session.monitor_id = self.capturer.primary_monitor_id()
                            monitor = self.capturer.monitor(session.monitor_id)
                            await ws.send_json({
                                "type": "auth_ok",
                                "platform": sys.platform,
                                "width": monitor.width,
                                "height": monitor.height,
                                "monitors": [m.public_dict() for m in monitors],
                                "upload_token": session.upload_token,
                                "ice_servers": configured_ice_servers(),
                            })
                        else:
                            session.auth_failures += 1
                            await asyncio.sleep(min(2.0, 0.25 * session.auth_failures))
                            await ws.send_json({"type": "auth_fail", "message": "Invalid PIN"})
                            if session.auth_failures >= 5:
                                await ws.close(code=1008, message=b"Too many authentication failures")

                    elif session.authenticated:
                        if msg_type == "frame_ack":
                            session.frame_ack_event.set()
                        elif msg_type == "ping":
                            await ws.send_json({"type": "pong", "time": data.get("time")})
                        else:
                            try:
                                await self.handle_input_message(data, ws, session)
                            except (TypeError, ValueError) as error:
                                await ws.send_json({"type": "input_error", "message": str(error)})

                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            await self._set_camera_subscription(session, False, notify=False)
            await self._set_microphone_subscription(session, False, notify=False)
            await self.webrtc.close_session(session)
            if session.media_ws and not session.media_ws.closed:
                await session.media_ws.close()
            if session.mouse_move_task:
                session.mouse_move_task.cancel()
            session.typing_cancel.set()
            if session.typing_task:
                session.typing_task.cancel()
            self._release_inputs(session)
            if session.upload_token:
                self.upload_tokens.discard(session.upload_token)
                self.token_sessions.pop(session.upload_token, None)
            self.clients.pop(ws, None)
            if not any(client.authenticated for client in self.clients.values()):
                await asyncio.to_thread(self.capture_hub.stop)
        return ws

    async def media_ws_handler(self, request):
        """Bulk screen/camera/audio channel, isolated from interactive input."""
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text="WebSocket origin does not match the host")
        ws = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024, heartbeat=15)
        await ws.prepare(request)
        try:
            auth_message = await asyncio.wait_for(ws.receive(), timeout=5)
            if auth_message.type != WSMsgType.TEXT:
                raise ValueError("Media authentication required")
            auth_data = json.loads(auth_message.data)
            if not isinstance(auth_data, dict) or auth_data.get("type") != "media_auth":
                raise ValueError("Media authentication required")
            session = self.token_sessions.get(str(auth_data.get("token", "")))
            if session is None or not session.authenticated:
                raise ValueError("Invalid media token")
        except (asyncio.TimeoutError, json.JSONDecodeError, TypeError, ValueError):
            await ws.close(code=1008, message=b"Media authentication failed")
            return ws

        previous = session.media_ws
        if previous and previous is not ws and not previous.closed:
            await previous.close(code=1001, message=b"Media channel replaced")
        session.media_ws = ws
        ack_event = session.frame_ack_event
        ack_event.clear()
        stream_task = asyncio.create_task(self.stream_worker(ws, ack_event, session))

        if session.camera_enabled:
            await self._set_camera_subscription(session, True, notify=True)
        if session.microphone_enabled:
            await self._set_microphone_subscription(session, True, notify=True)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(data, dict) and data.get("type") == "frame_ack":
                        ack_event.set()
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
            if session.media_ws is ws:
                keep_camera = session.camera_enabled
                keep_microphone = session.microphone_enabled
                await self._set_camera_subscription(session, False, notify=False)
                await self._set_microphone_subscription(session, False, notify=False)
                session.camera_enabled = keep_camera
                session.microphone_enabled = keep_microphone
                session.media_ws = None
        return ws


def start_tunnel_prompt(port):
    """Print local connection information."""
    print("=" * 60)
    print(" [Project X] Server is running locally on http://localhost:" + str(port))
    print("=" * 60)
    print(" Run `python start.py` to create a managed Cloudflare tunnel.")
    print("=" * 60 + "\n")


def create_app(pin="1234", port=5000):
    server = RemoteServer(pin=pin, port=port)

    @web.middleware
    async def prevent_stale_viewer_assets(request, handler):
        if request.path != "/" and not request.path.startswith("/static/"):
            return await handler(request)
        response = await handler(request)
        # A viewer can remain open while the host is upgraded. On its next
        # reload it must fetch the matching HTML, JavaScript, and CSS rather
        # than revive an obsolete latency pipeline from browser cache.
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    app = web.Application(middlewares=[prevent_stale_viewer_assets])
    app.router.add_get("/ws", server.ws_handler)
    app.router.add_get("/ws/media", server.media_ws_handler)
    
    # Static files & Uploads directory
    base_dir = getattr(
        sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))
    )
    static_dir = os.path.join(base_dir, "static")
    uploads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(static_dir, exist_ok=True)
    os.makedirs(uploads_dir, exist_ok=True)
    
    async def index_handler(request):
        return web.FileResponse(os.path.join(static_dir, "index.html"))

    async def upload_handler(request):
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        if not token or token not in server.upload_tokens:
            return web.json_response(
                {"status": "error", "message": "Authentication required"},
                status=401,
            )

        max_upload_size = 100 * 1024 * 1024
        if request.content_length and request.content_length > max_upload_size:
            return web.json_response(
                {"status": "error", "message": "File exceeds the 100 MB limit"},
                status=413,
            )

        reader = await request.multipart()
        field = await reader.next()
        if field and field.filename:
            filename = os.path.basename(field.filename)
            saved_filename = f"{secrets.token_hex(4)}-{filename}"
            filepath = os.path.join(uploads_dir, saved_filename)
            size = 0
            async with aiofiles.open(filepath, "xb") as destination:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_upload_size:
                        await destination.close()
                        await asyncio.to_thread(os.remove, filepath)
                        return web.json_response(
                            {"status": "error", "message": "File exceeds the 100 MB limit"},
                            status=413,
                        )
                    await destination.write(chunk)
            logger.info("Received file: %s (%d bytes) -> %s", filename, size, filepath)
            return web.json_response({
                "status": "ok",
                "filename": filename,
                "saved_as": saved_filename,
            })
        return web.json_response({"status": "error", "message": "No file uploaded"}, status=400)

    async def webrtc_offer_handler(request):
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        session = server.token_sessions.get(token)
        if session is None:
            return web.json_response(
                {"status": "error", "message": "Authentication required"},
                status=401,
            )
        try:
            payload = await request.json()
            if payload.get("type") != "offer" or not payload.get("sdp"):
                raise ValueError("A valid WebRTC offer is required")
            answer = await server.webrtc.offer(payload, session)
            return web.json_response(answer)
        except (ValueError, KeyError, TypeError) as error:
            return web.json_response(
                {"status": "error", "message": str(error)},
                status=400,
            )

    async def performance_handler(request):
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        session = server.token_sessions.get(token)
        if session is None or not session.authenticated:
            return web.json_response(
                {"status": "error", "message": "Authentication required"},
                status=401,
            )
        return web.json_response(server.performance_snapshot())

    app.router.add_get("/", index_handler)
    app.router.add_post("/api/upload", upload_handler)
    app.router.add_post("/api/webrtc/offer", webrtc_offer_handler)
    app.router.add_get("/api/performance", performance_handler)
    app.router.add_static("/static/", static_dir)

    async def cleanup(_app):
        await server.webrtc.close()
        await asyncio.to_thread(server.camera.stop)
        await asyncio.to_thread(server.microphone.stop)
        await asyncio.to_thread(server.capture_hub.stop)

    app.on_cleanup.append(cleanup)
    return app, server


def run_server(host="127.0.0.1", port=5000, pin="1234", request_permissions=True):
    """Run the host server from source or a frozen Project X executable."""
    acquire_host_lock()
    if sys.platform == "darwin":
        from macos_permissions import check_macos_permissions

        permissions = check_macos_permissions(request=request_permissions)
        if not permissions["accessibility"]:
            print("PROJECTX_PERMISSION_REQUIRED:accessibility", flush=True)
        if not permissions["screen_recording"]:
            print("PROJECTX_PERMISSION_REQUIRED:screen_recording", flush=True)
            raise RuntimeError(
                "Screen Recording permission is required; wallpaper-only fallback disabled"
            )

    start_tunnel_prompt(port)
    app, _ = create_app(pin=pin, port=port)
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Project X - Remote Desktop Server")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 intentionally for LAN access)",
    )
    parser.add_argument("--port", type=int, default=5000, help="Local port (default: 5000)")
    parser.add_argument("--pin", type=str, default="1234", help="Security PIN (default: 1234)")
    parser.add_argument(
        "--no-permission-prompt",
        action="store_true",
        help="Do not open macOS privacy permission prompts/settings",
    )
    args = parser.parse_args()

    try:
        run_server(
            host=args.host,
            port=args.port,
            pin=args.pin,
            request_permissions=not args.no_permission_prompt,
        )
    except RuntimeError as error:
        parser.error(str(error))
