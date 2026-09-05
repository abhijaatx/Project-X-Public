import unittest
from unittest.mock import patch
from argparse import Namespace

import start


class LauncherTests(unittest.TestCase):
    def test_generated_pin_is_eight_numeric_characters(self):
        pin = start.generated_pin()
        self.assertEqual(len(pin), 8)
        self.assertTrue(pin.isdigit())

    @patch("start.shutil.which", return_value="/opt/homebrew/bin/cloudflared")
    @patch("start.os.path.isfile", return_value=False)
    def test_cloudflared_can_be_discovered_on_path(self, _isfile, _which):
        self.assertEqual(start.find_cloudflared(), "/opt/homebrew/bin/cloudflared")

    @patch.object(start.sys, "frozen", True, create=True)
    def test_frozen_launcher_uses_internal_server_child(self):
        args = Namespace(host="127.0.0.1", port=5000)
        command = start.server_command(args, "12345678")
        self.assertEqual(command[1], "--server-child")
        self.assertIn("12345678", command)

    @patch.object(start.sys, "platform", "linux")
    @patch("start.socket.socket")
    def test_lan_address_uses_active_interface(self, socket_factory):
        probe = socket_factory.return_value
        probe.getsockname.return_value = ("192.168.1.25", 49152)
        self.assertEqual(start.lan_address(5001), "http://192.168.1.25:5001")
        probe.close.assert_called_once()

    @patch.object(start.sys, "platform", "darwin")
    @patch("start.subprocess.run")
    @patch("start.socket.socket")
    def test_macos_lan_address_ignores_vpn_default_route(
        self, socket_factory, run
    ):
        run.return_value.stdout = "10.2.8.21\n"

        self.assertEqual(start.lan_address(5001), "http://10.2.8.21:5001")
        run.assert_called_once()
        socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
