import unittest
from unittest.mock import patch

import numpy as np

from capture_backends import MSSScreenCapture


class FakeMonitor:
    def __init__(self, left, top, width, height, *, primary=False, name=None):
        self.left = left
        self.top = top
        self.width = width
        self.height = height
        self.is_primary = primary
        self.name = name


class FakeShot:
    def __init__(self, width, height):
        self.pixels = np.zeros((height, width, 4), dtype=np.uint8)

    def __array__(self, dtype=None, copy=None):
        return np.asarray(self.pixels, dtype=dtype)


class FakeMSS:
    def __init__(self, **_kwargs):
        virtual = FakeMonitor(-640, 0, 1280, 480)
        primary = FakeMonitor(0, 0, 640, 480, primary=True, name="Main")
        secondary = FakeMonitor(-640, 0, 640, 480, name="Side")
        self.monitors = [virtual, primary, secondary]
        self.primary_monitor = primary

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def grab(self, monitor):
        return FakeShot(monitor.width, monitor.height)


class CaptureBackendTests(unittest.TestCase):
    @patch("capture_backends.MSS", FakeMSS)
    def test_enumerates_monitor_offsets_and_primary_display(self):
        capture = MSSScreenCapture()
        monitors = capture.monitors()

        self.assertEqual(len(monitors), 2)
        self.assertEqual(capture.primary_monitor_id(), 1)
        self.assertEqual(monitors[1].left, -640)
        self.assertEqual(monitors[1].name, "Side")

    @patch("capture_backends.MSS", FakeMSS)
    def test_captures_scaled_jpeg_for_selected_monitor(self):
        capture = MSSScreenCapture()
        jpeg, monitor = capture.grab_jpeg(2, quality=50, scale=0.5)

        self.assertEqual(monitor.id, 2)
        self.assertIsNotNone(jpeg)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
