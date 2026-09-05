import json
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server import ClientSession, RemoteServer, configured_ice_servers


class RealtimeInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_precision_scroll_accumulates_before_windows_wheel_step(self):
        server = RemoteServer.__new__(RemoteServer)
        session = ClientSession(monitor_id=1)

        with patch("server.macos_input", None), patch("server.mouse.scroll") as scroll:
            for _ in range(4):
                await RemoteServer.handle_input_message(
                    server,
                    {"type": "mouse_wheel", "deltaY": 20, "deltaMode": 0},
                    None,
                    session,
                )
            scroll.assert_not_called()

            await RemoteServer.handle_input_message(
                server,
                {"type": "mouse_wheel", "deltaY": 20, "deltaMode": 0},
                None,
                session,
            )
            scroll.assert_called_once_with(0, -1)
            self.assertAlmostEqual(session.scroll_remainder, 0.0)

    async def test_windows_scroll_caps_large_browser_delta(self):
        server = RemoteServer.__new__(RemoteServer)
        session = ClientSession(monitor_id=1)

        with patch("server.macos_input", None), patch("server.mouse.scroll") as scroll:
            await RemoteServer.handle_input_message(
                server,
                {"type": "mouse_wheel", "deltaY": -2000, "deltaMode": 0},
                None,
                session,
            )

            scroll.assert_called_once_with(0, 3)

    async def test_jpeg_stream_limits_unacknowledged_frames_and_resumes(self):
        class Transport:
            def get_write_buffer_size(self):
                return 0

        class Writer:
            transport = Transport()

        class Socket:
            _writer = Writer()

            def __init__(self):
                self.closed = False
                self.frames = []

            async def send_bytes(self, payload):
                self.frames.append(payload)
                if len(self.frames) >= RemoteServer.MAX_JPEG_FRAMES_IN_FLIGHT + 1:
                    self.closed = True

        class Server:
            MEDIA_WRITE_BUFFER_LIMIT = RemoteServer.MEDIA_WRITE_BUFFER_LIMIT
            MAX_JPEG_FRAMES_IN_FLIGHT = RemoteServer.MAX_JPEG_FRAMES_IN_FLIGHT

            def __init__(self):
                self.calls = 0

            def capture_frame(self, _session, sequence):
                self.calls += 1
                return b"jpeg", sequence + 1

        server = Server()
        socket = Socket()
        session = SimpleNamespace(use_webrtc=False, target_fps=60)
        ack_event = asyncio.Event()
        task = asyncio.create_task(
            RemoteServer.stream_worker(server, socket, ack_event, session)
        )
        await asyncio.sleep(0.15)
        self.assertEqual(
            len(socket.frames), RemoteServer.MAX_JPEG_FRAMES_IN_FLIGHT
        )
        ack_event.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(
            len(socket.frames), RemoteServer.MAX_JPEG_FRAMES_IN_FLIGHT + 1
        )

    async def test_rising_route_delay_triggers_media_backpressure(self):
        class WebRTC:
            def __init__(self):
                self.degraded = []

            def set_session_media_health(self, _session, degraded):
                self.degraded.append(degraded)

        server = RemoteServer.__new__(RemoteServer)
        server.webrtc = WebRTC()
        session = ClientSession(monitor_id=1)
        ws = AsyncMock()
        healthy = {
            "type": "media_stats",
            "packetLoss": 0,
            "jitter": 0.005,
            "fps": 30,
            "routeRtt": 0.01,
            "routeType": "host",
        }
        queued = {**healthy, "routeRtt": 0.25}

        await RemoteServer.handle_input_message(server, healthy, ws, session)
        await RemoteServer.handle_input_message(server, queued, ws, session)
        await RemoteServer.handle_input_message(server, queued, ws, session)

        self.assertEqual(session.best_route_rtt_ms, 10.0)
        self.assertEqual(session.last_route_rtt_ms, 250.0)
        self.assertLess(session.webrtc_scale, session.requested_scale)
        self.assertEqual(session.target_fps, 15)
        self.assertTrue(server.webrtc.degraded[-1])

    async def test_reliable_channel_executes_and_acknowledges_input(self):
        handler = AsyncMock()
        server = SimpleNamespace(handle_input_message=handler)
        session = SimpleNamespace(
            authenticated=True,
            control_ws=object(),
            input_count=0,
            input_processing_total_ms=0.0,
            input_processing_max_ms=0.0,
        )

        class Channel:
            label = "projectx-control"
            readyState = "open"

            def __init__(self):
                self.messages = []

            def send(self, message):
                self.messages.append(json.loads(message))

        channel = Channel()
        data = {"type": "key_down", "key": "a", "code": "KeyA", "input_id": 7}

        await RemoteServer._handle_realtime_channel_input(
            server, data, session, channel
        )

        handler.assert_awaited_once_with(data, session.control_ws, session)
        self.assertEqual(channel.messages[0]["type"], "input_ack")
        self.assertEqual(channel.messages[0]["input_id"], 7)
        self.assertGreaterEqual(channel.messages[0]["server_ms"], 0)
        self.assertEqual(session.input_count, 1)

    async def test_unreliable_pointer_channel_rejects_keyboard_events(self):
        handler = AsyncMock()
        server = SimpleNamespace(handle_input_message=handler)
        session = SimpleNamespace(authenticated=True, control_ws=object())
        channel = SimpleNamespace(
            label="projectx-pointer",
            readyState="open",
            send=lambda _message: None,
        )

        await RemoteServer._handle_realtime_channel_input(
            server,
            {"type": "key_down", "key": "a"},
            session,
            channel,
        )

        handler.assert_not_awaited()

    def test_turn_configuration_is_only_exposed_when_configured(self):
        self.assertEqual(configured_ice_servers({}), [])
        self.assertEqual(
            configured_ice_servers({
                "PROJECTX_TURN_URLS": "turn:relay.example:3478,turns:relay.example:5349",
                "PROJECTX_TURN_USERNAME": "projectx",
                "PROJECTX_TURN_CREDENTIAL": "secret",
            }),
            [{
                "urls": [
                    "turn:relay.example:3478",
                    "turns:relay.example:5349",
                ],
                "username": "projectx",
                "credential": "secret",
            }],
        )


if __name__ == "__main__":
    unittest.main()
