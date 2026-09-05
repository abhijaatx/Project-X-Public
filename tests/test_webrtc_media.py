import fractions
import asyncio
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import av
import numpy as np

from webrtc_media import (
    VIDEO_TOOLBOX_AVAILABLE,
    VideoToolboxH264Encoder,
    WindowsH264Encoder,
    WebRTCManager,
    SharedScreenTrack,
    _prefer_direct_lan_sdp,
    _windows_h264_candidates,
)
from shared_capture import FrameSnapshot
from capture_backends import MonitorGeometry


class WebRTCMediaTests(unittest.TestCase):
    def test_direct_lan_candidate_excludes_competing_vpn_interfaces(self):
        sdp = (
            "v=0\r\n"
            "a=candidate:lan 1 udp 2130706431 10.2.8.21 50000 typ host\r\n"
            "a=candidate:vpn 1 udp 2130706431 10.8.0.4 50001 typ host\r\n"
            "a=candidate:wan 1 udp 1677730815 203.0.113.5 50002 typ srflx\r\n"
            "a=end-of-candidates\r\n"
        )

        filtered, preferred = _prefer_direct_lan_sdp(sdp, "10.2.8.129")

        self.assertEqual(preferred, "10.2.8.21")
        self.assertIn("10.2.8.21", filtered)
        self.assertNotIn("10.8.0.4", filtered)
        self.assertIn("203.0.113.5", filtered)
        self.assertTrue(filtered.endswith("\r\n"))

    def test_routed_campus_subnet_uses_physical_lan_candidate(self):
        sdp = (
            "v=0\r\n"
            "a=candidate:lan 1 udp 2130706431 10.2.8.21 50000 typ host\r\n"
            "a=candidate:vpn 1 udp 2130706431 10.8.1.11 50001 typ host\r\n"
        )

        filtered, preferred = _prefer_direct_lan_sdp(sdp, "10.2.21.119")

        self.assertEqual(preferred, "10.2.8.21")
        self.assertIn("10.2.8.21", filtered)
        self.assertNotIn("10.8.1.11", filtered)

    def test_candidate_filter_leaves_sdp_unchanged_without_lan_match(self):
        sdp = "v=0\r\na=candidate:vpn 1 udp 1 10.8.0.4 50001 typ host\r\n"
        filtered, preferred = _prefer_direct_lan_sdp(sdp, "10.2.8.129")
        self.assertIsNone(preferred)
        self.assertEqual(filtered, sdp)

    def test_lan_bitrate_adapts_down_quickly_and_recovers_slowly(self):
        encoder = VideoToolboxH264Encoder()
        encoder.allow_bitrate_downshift = True
        with patch("webrtc_media.time.monotonic", return_value=10.0):
            encoder.target_bitrate = 1_000_000
        self.assertEqual(encoder.target_bitrate, 1_000_000)

        with patch("webrtc_media.time.monotonic", return_value=12.0):
            encoder.target_bitrate = 2_000_000
        self.assertEqual(encoder.target_bitrate, 1_000_000)

        with patch("webrtc_media.time.monotonic", return_value=19.0):
            encoder.target_bitrate = 2_000_000
        self.assertEqual(encoder.target_bitrate, 1_200_000)

    def test_bitrate_ignores_conservative_startup_remb(self):
        encoder = VideoToolboxH264Encoder()
        with patch("webrtc_media.time.monotonic", return_value=10.0):
            encoder.target_bitrate = 800_000
        self.assertEqual(encoder.target_bitrate, encoder.INITIAL_BITRATE)

    def test_windows_bitrate_adapts_without_recreating_on_every_report(self):
        encoder = WindowsH264Encoder(codec_name="libx264")
        encoder.allow_bitrate_downshift = True
        self.assertEqual(encoder.target_bitrate, 2_000_000)

        with patch("webrtc_media.time.monotonic", return_value=10.0):
            encoder.target_bitrate = 1_000_000
        self.assertEqual(encoder.target_bitrate, 1_300_000)

        # Recovery is intentionally slower than a downshift.
        with patch("webrtc_media.time.monotonic", return_value=12.0):
            encoder.target_bitrate = 4_000_000
        self.assertEqual(encoder.target_bitrate, 1_300_000)
        with patch("webrtc_media.time.monotonic", return_value=19.0):
            encoder.target_bitrate = 4_000_000
        self.assertEqual(encoder.target_bitrate, 1_560_000)

    def test_windows_x264_fallback_packetizes_low_delay_h264(self):
        encoder = WindowsH264Encoder(codec_name="libx264")
        payloads = []
        for pts in range(3):
            frame = av.VideoFrame.from_ndarray(
                np.zeros((240, 320, 4), dtype=np.uint8),
                format="bgra",
            )
            frame.pts = pts
            frame.time_base = fractions.Fraction(1, 30)
            encoded, _timestamp = encoder.encode(frame, force_keyframe=pts == 0)
            payloads.extend(encoded)

        self.assertEqual(encoder.active_codec_name, "libx264")
        self.assertTrue(payloads)
        self.assertLessEqual(max(map(len, payloads)), 1300)


class WebRTCLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_track_honors_selected_frame_rate(self):
        track = SharedScreenTrack(
            capture_hub=None,
            client_session=SimpleNamespace(target_fps=30),
        )
        loop = asyncio.get_running_loop()
        start = loop.time()
        await track._wait_for_frame_slot()
        await track._wait_for_frame_slot()
        self.assertGreaterEqual(loop.time() - start, 0.02)

    async def test_screen_track_uses_relative_shared_media_timestamp(self):
        monitor = MonitorGeometry(1, 0, 0, 320, 240, True)
        pixels = np.zeros((240, 320, 4), dtype=np.uint8)
        snapshot = FrameSnapshot(
            sequence=2,
            captured_at=100.1,
            bgra=pixels,
            monitor=monitor,
        )

        class Hub:
            media_epoch = 100.0

            def wait_for_frame(self, *_args, **_kwargs):
                return snapshot

        track = SharedScreenTrack(
            capture_hub=Hub(),
            client_session=SimpleNamespace(
                monitor_id=1,
                target_fps=30,
                webrtc_scale=1.0,
            ),
        )
        frame = await track.recv()

        self.assertEqual(frame.pts, 9_000)
        self.assertEqual(frame.time_base, fractions.Fraction(1, 90_000))

    async def test_closing_session_releases_all_of_its_peers(self):
        class FakePeer:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        manager = WebRTCManager(capture_hub=None)
        session = SimpleNamespace(use_webrtc=True)
        peer = FakePeer()
        manager.peer_connections.add(peer)
        manager.session_peers[id(session)] = {peer}

        await manager.close_session(session)

        self.assertTrue(peer.closed)
        self.assertFalse(session.use_webrtc)
        self.assertNotIn(peer, manager.peer_connections)
        self.assertNotIn(id(session), manager.session_peers)

    def test_windows_hardware_probe_keeps_supported_encoder_order(self):
        def fake_codec(name, _mode):
            if name in {"h264_qsv", "h264_amf"}:
                return object()
            raise ValueError(name)

        with (
            patch("webrtc_media.sys.platform", "win32"),
            patch("webrtc_media.av.Codec", side_effect=fake_codec),
        ):
            self.assertEqual(
                _windows_h264_candidates(),
                ("h264_qsv", "h264_amf"),
            )

    @unittest.skipUnless(VIDEO_TOOLBOX_AVAILABLE, "VideoToolbox is macOS-only")
    def test_videotoolbox_encoder_packetizes_h264(self):
        VideoToolboxH264Encoder.clear_shared_pool()
        encoder = VideoToolboxH264Encoder()
        payloads = []
        for pts in range(4):
            frame = av.VideoFrame.from_ndarray(
                np.zeros((480, 640, 3), dtype=np.uint8),
                format="bgr24",
            )
            frame.pts = pts
            frame.time_base = fractions.Fraction(1, 30)
            encoded, _timestamp = encoder.encode(frame, force_keyframe=pts == 0)
            payloads.extend(encoded)

        self.assertTrue(payloads)
        self.assertLessEqual(max(map(len, payloads)), 1300)

    @unittest.skipUnless(VIDEO_TOOLBOX_AVAILABLE, "VideoToolbox is macOS-only")
    def test_identical_viewer_frames_reuse_videotoolbox_encode(self):
        VideoToolboxH264Encoder.clear_shared_pool()
        first = VideoToolboxH264Encoder()
        second = VideoToolboxH264Encoder()
        frame_one = av.VideoFrame.from_ndarray(
            np.zeros((240, 320, 4), dtype=np.uint8), format="bgra"
        )
        frame_one.pts = 9000
        frame_one.time_base = fractions.Fraction(1, 90_000)
        frame_two = av.VideoFrame.from_ndarray(
            np.zeros((240, 320, 4), dtype=np.uint8), format="bgra"
        )
        frame_two.pts = frame_one.pts
        frame_two.time_base = frame_one.time_base

        first_payloads, _ = first.encode(frame_one, force_keyframe=True)
        second_payloads, _ = second.encode(frame_two, force_keyframe=True)
        stats = VideoToolboxH264Encoder.shared_pool_stats()

        self.assertTrue(first_payloads)
        self.assertEqual(first_payloads, second_payloads)
        self.assertEqual(stats["encodes"], 1)
        self.assertEqual(stats["cache_hits"], 1)


if __name__ == "__main__":
    unittest.main()
