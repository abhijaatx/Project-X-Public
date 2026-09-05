import threading
import unittest

from pynput.keyboard import Key

from server import RemoteServer


class FakeKeyboard:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", key))

    def release(self, key):
        self.events.append(("release", key))


class VirtualCancelEvent:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def is_set(self):
        return False

    def wait(self, timeout):
        self.waits.append(timeout)
        self.now += timeout
        return False


class TextInjectionTests(unittest.TestCase):
    def test_preserves_newlines_tabs_and_carriage_returns(self):
        keyboard = FakeKeyboard()

        typed, skipped = RemoteServer._type_text(
            keyboard, "first\r\nsecond\tvalue", threading.Event(), 1000
        )

        self.assertEqual(typed, len("first\nsecond\tvalue"))
        self.assertEqual(skipped, 0)
        self.assertEqual(keyboard.events, [
        ("press", "f"),
        ("release", "f"),
        ("press", "i"),
        ("release", "i"),
        ("press", "r"),
        ("release", "r"),
        ("press", "s"),
        ("release", "s"),
        ("press", "t"),
        ("release", "t"),
        ("press", Key.enter),
        ("release", Key.enter),
        ("press", "s"),
        ("release", "s"),
        ("press", "e"),
        ("release", "e"),
        ("press", "c"),
        ("release", "c"),
        ("press", "o"),
        ("release", "o"),
        ("press", "n"),
        ("release", "n"),
        ("press", "d"),
        ("release", "d"),
        ("press", Key.tab),
        ("release", Key.tab),
        ("press", "v"),
        ("release", "v"),
        ("press", "a"),
        ("release", "a"),
        ("press", "l"),
        ("release", "l"),
        ("press", "u"),
        ("release", "u"),
        ("press", "e"),
        ("release", "e"),
        ])

    def test_honors_cancellation_before_injection(self):
        keyboard = FakeKeyboard()
        cancelled = threading.Event()
        cancelled.set()

        typed, skipped = RemoteServer._type_text(
            keyboard, "do not type", cancelled, 300
        )

        self.assertEqual((typed, skipped), (0, 0))
        self.assertEqual(keyboard.events, [])

    def test_paces_characters_at_requested_rate(self):
        keyboard = FakeKeyboard()
        timer = VirtualCancelEvent()

        typed, skipped = RemoteServer._type_text(
            keyboard,
            "abcd",
            timer,
            100,
            clock=lambda: timer.now,
            random_uniform=lambda _low, _high: 1.0,
        )

        self.assertEqual((typed, skipped), (4, 0))
        self.assertEqual(len(timer.waits), 3)
        self.assertAlmostEqual(sum(timer.waits), 0.03)

    def test_applies_bounded_random_jitter(self):
        keyboard = FakeKeyboard()
        timer = VirtualCancelEvent()
        factors = iter((0.88, 1.0, 1.12))

        RemoteServer._type_text(
            keyboard,
            "abcd",
            timer,
            100,
            clock=lambda: timer.now,
            random_uniform=lambda _low, _high: next(factors),
        )

        expected_waits = (0.0088, 0.01, 0.0112)
        for actual, expected in zip(timer.waits, expected_waits):
            self.assertAlmostEqual(actual, expected)

    def test_code_mode_expands_tabs_and_clears_editor_auto_indent(self):
        keyboard = FakeKeyboard()
        timer = VirtualCancelEvent()

        typed, skipped = RemoteServer._type_text(
            keyboard,
            "\tfoo\n\tbar",
            timer,
            40,
            clock=lambda: timer.now,
            random_uniform=lambda _low, _high: 1.0,
            code_mode=True,
            platform_name="darwin",
        )

        self.assertEqual((typed, skipped), (15, 0))
        self.assertNotIn(("press", Key.tab), keyboard.events)
        newline_index = keyboard.events.index(("press", Key.enter))
        self.assertEqual(
            keyboard.events[newline_index:newline_index + 6],
            [
                ("press", Key.enter),
                ("release", Key.enter),
                ("press", Key.cmd),
                ("press", Key.left),
                ("release", Key.left),
                ("release", Key.cmd),
            ],
        )


if __name__ == "__main__":
    unittest.main()
