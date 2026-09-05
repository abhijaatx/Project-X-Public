import json
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from server import RemoteServer, configured_ice_servers


class RealtimeInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_jpeg_stream_does_not_wait_for_render_ack(self):
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
                if len(self.frames) >= 2:
                    self.closed = True

        class Server:
            MEDIA_WRITE_BUFFER_LIMIT = RemoteServer.MEDIA_WRITE_BUFFER_LIMIT

            def __init__(self):
                self.calls = 0

            def capture_frame(self, _session, sequence):
                self.calls += 1
                return b"jpeg", sequence + 1

        server = Server()
        socket = Socket()
        session = SimpleNamespace(use_webrtc=False, target_fps=30)
        # The old implementation would block indefinitely until this event was
        # set by a browser render ACK.  Leave it unset to exercise the new path.
        ack_event = asyncio.Event()

        await asyncio.wait_for(
            RemoteServer.stream_worker(server, socket, ack_event, session),
            timeout=1,
        )

        self.assertGreaterEqual(len(socket.frames), 2)
        self.assertGreaterEqual(server.calls, 2)

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
