import unittest
import threading
import time
from unittest.mock import patch

import numpy as np

from capture_backends import MonitorGeometry
from shared_capture import (
    SharedCaptureHub,
    _DXCamPollingStream,
    _LatestFrameStore,
    _scale_bgra,
)


class SharedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.monitor = MonitorGeometry(1, 0, 0, 640, 480, True, "Main")
        self.store = _LatestFrameStore(self.monitor)

    def test_latest_frame_replaces_stale_frame(self):
        self.store.publish(np.zeros((480, 640, 4), dtype=np.uint8))
        first = self.store.wait()
        self.store.publish(np.full((480, 640, 4), 255, dtype=np.uint8))
        second = self.store.wait(after_sequence=first.sequence)

        self.assertEqual(second.sequence, first.sequence + 1)
        self.assertEqual(int(second.bgra[0, 0, 0]), 255)

    def test_identical_jpeg_variant_is_cached(self):
        self.store.publish(np.zeros((480, 640, 4), dtype=np.uint8))
        snapshot = self.store.wait()

        first = self.store.jpeg(snapshot, quality=45, scale=0.7)
        second = self.store.jpeg(snapshot, quality=45, scale=0.7)

        self.assertIs(first, second)
        self.assertTrue(first.startswith(b"\xff\xd8"))

    def test_native_scaler_preserves_bgra_shape_and_channel_order(self):
        frame = np.zeros((4, 4, 4), dtype=np.uint8)
        frame[:, :, 0] = 17
        frame[:, :, 1] = 31
        frame[:, :, 2] = 47
        frame[:, :, 3] = 255

        scaled = _scale_bgra(frame, 2, 2)

        self.assertEqual(scaled.shape, (2, 2, 4))
        self.assertEqual(scaled[0, 0].tolist(), [17, 31, 47, 255])

    def test_cross_platform_scaler_preserves_bgra_pixels(self):
        frame = np.zeros((8, 8, 4), dtype=np.uint8)
        frame[:, :, :] = [13, 29, 61, 255]
        with patch("shared_capture._VIMAGE_SCALE", None):
            scaled = _scale_bgra(frame, 4, 4)
        self.assertEqual(scaled.shape, (4, 4, 4))
        self.assertEqual(scaled[0, 0].tolist(), [13, 29, 61, 255])

    def test_dxcam_uses_borderless_dxgi_bgra_video_mode_and_latest_frame(self):
        class FakeCamera:
            def __init__(self):
                self.start_options = None
                self.stopped = False

            def start(self, **options):
                self.start_options = options

            def get_latest_frame(self):
                return np.full((480, 640, 4), 77, dtype=np.uint8)

            def stop(self):
                self.stopped = True

        camera = FakeCamera()

        class FakeDXCam:
            def __init__(self):
                self.create_options = None

            def create(self, **options):
                self.create_options = options
                return camera

        fake_dxcam = FakeDXCam()
        stream = _DXCamPollingStream(self.monitor, fps=30)
        with patch("shared_capture.dxcam", fake_dxcam, create=True):
            stream.start()
            snapshot = stream.store.wait(timeout=1)
            stream.stop()

        self.assertEqual(fake_dxcam.create_options["backend"], "dxgi")
        self.assertEqual(fake_dxcam.create_options["output_color"], "BGRA")
        self.assertEqual(fake_dxcam.create_options["processor_backend"], "numpy")
        self.assertEqual(fake_dxcam.create_options["max_buffer_len"], 2)
        self.assertEqual(camera.start_options["target_fps"], 30)
        self.assertTrue(camera.start_options["video_mode"])
        self.assertEqual(int(snapshot.bgra[0, 0, 0]), 77)
        self.assertTrue(camera.stopped)

    def test_winrt_capture_requires_explicit_opt_in(self):
        stream = _DXCamPollingStream(self.monitor, fps=30, backend_preference="winrt")
        self.assertEqual(stream.backends, ("winrt", "dxgi"))

    def test_stalled_capture_recovers_with_replacement_stream(self):
        class FakeStream:
            def __init__(self, monitor):
                self.store = _LatestFrameStore(monitor)

        stalled = FakeStream(self.monitor)
        stalled.store.publish(np.zeros((480, 640, 4), dtype=np.uint8))
        stalled.store.captured_at = time.perf_counter() - 5
        replacement = FakeStream(self.monitor)
        replacement.store.publish(np.full((480, 640, 4), 99, dtype=np.uint8))

        hub = SharedCaptureHub.__new__(SharedCaptureHub)
        hub.fps = 30
        hub.lock = threading.Lock()
        hub.restart_lock = threading.Lock()
        hub.streams = {1: stalled}
        hub._stream = lambda _monitor_id: stalled
        hub._restart_stalled_stream = lambda _monitor_id, _stream: replacement

        stream, snapshot = hub._wait_with_recovery(1, 1, 0.01)

        self.assertIs(stream, replacement)
        self.assertEqual(int(snapshot.bgra[0, 0, 0]), 99)

    def test_idle_screen_is_not_mistaken_for_a_stalled_capture(self):
        class FakeStream:
            def __init__(self, monitor):
                self.store = _LatestFrameStore(monitor)
                self.is_idle = True
                self.non_idle_statuses = 0
                self.last_status_at = time.perf_counter()

        idle = FakeStream(self.monitor)
        idle.store.publish(np.zeros((480, 640, 4), dtype=np.uint8))
        idle.store.captured_at = time.perf_counter() - 5

        hub = SharedCaptureHub.__new__(SharedCaptureHub)
        hub.fps = 30
        hub.lock = threading.Lock()
        hub.restart_lock = threading.Lock()
        hub.streams = {1: idle}
        hub._stream = lambda _monitor_id: idle
        restarts = []
        hub._restart_stalled_stream = (
            lambda _monitor_id, _stream: restarts.append(True)
        )

        stream, snapshot = hub._wait_with_recovery(1, 1, 0.001)

        self.assertIs(stream, idle)
        self.assertIsNone(snapshot)
        self.assertEqual(restarts, [])

    def test_idle_state_does_not_hide_a_stopped_callback_stream(self):
        class FakeStream:
            def __init__(self, monitor):
                self.store = _LatestFrameStore(monitor)
                self.is_idle = True
                self.non_idle_statuses = 0
                self.last_status_at = time.perf_counter() - 5

        stopped = FakeStream(self.monitor)
        stopped.store.publish(np.zeros((480, 640, 4), dtype=np.uint8))
        stopped.store.captured_at = time.perf_counter() - 5
        replacement = FakeStream(self.monitor)
        replacement.is_idle = False
        replacement.store.publish(np.full((480, 640, 4), 83, dtype=np.uint8))

        hub = SharedCaptureHub.__new__(SharedCaptureHub)
        hub.fps = 30
        hub.lock = threading.Lock()
        hub.restart_lock = threading.Lock()
        hub.streams = {1: stopped}
        hub._stream = lambda _monitor_id: stopped
        hub._restart_stalled_stream = lambda _monitor_id, _stream: replacement

        stream, snapshot = hub._wait_with_recovery(1, 1, 0.001)

        self.assertIs(stream, replacement)
        self.assertEqual(int(snapshot.bgra[0, 0, 0]), 83)


if __name__ == "__main__":
    unittest.main()
