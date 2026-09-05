import unittest
from unittest.mock import patch

import desktop_app


class DesktopAppTests(unittest.TestCase):
    def test_lan_is_the_default_desktop_mode(self):
        self.assertEqual(desktop_app.DEFAULT_MODE, "lan")

    @patch.object(desktop_app.sys, "frozen", True, create=True)
    @patch.object(desktop_app.sys, "platform", "darwin")
    def test_macos_frozen_app_must_run_from_applications(self):
        self.assertFalse(
            desktop_app.is_macos_app_installed(
                "/Volumes/Project X/Project X.app/Contents/MacOS/Project X"
            )
        )
        self.assertTrue(
            desktop_app.is_macos_app_installed(
                "/Applications/Project X.app/Contents/MacOS/Project X"
            )
        )

    @patch.object(desktop_app.sys, "frozen", True, create=True)
    def test_frozen_tray_uses_internal_host_child(self):
        command = desktop_app.child_command("tunnel", "87654321")
        self.assertEqual(command[1], "--host-child")
        self.assertIn("87654321", command)
        self.assertNotIn("--no-tunnel", command)

    @patch.object(desktop_app.sys, "frozen", True, create=True)
    def test_lan_child_is_bound_to_lan_port(self):
        command = desktop_app.child_command("lan", "87654321")
        self.assertIn("--no-tunnel", command)
        self.assertIn("0.0.0.0", command)
        self.assertIn("5001", command)

    def test_status_patterns_parse_launcher_output(self):
        self.assertEqual(
            desktop_app.URL_PATTERN.search(
                "Remote URL: https://example.trycloudflare.com"
            ).group(1),
            "https://example.trycloudflare.com",
        )
        self.assertEqual(
            desktop_app.PIN_PATTERN.search("PIN: 12345678").group(1),
            "12345678",
        )

    def test_permission_markers_are_exposed_in_tray_status(self):
        controller = desktop_app.ProjectXController()
        controller.mode = "lan"
        controller._handle_output_line("PROJECTX_PERMISSION_REQUIRED:accessibility")
        controller._handle_output_line("Local URL: http://172.20.10.4:5001")
        self.assertTrue(controller.missing_accessibility)
        self.assertEqual(
            controller.status, "Running on LAN · Accessibility required"
        )

        controller._handle_output_line("PROJECTX_PERMISSION_REQUIRED:screen_recording")
        self.assertEqual(controller.status, "Screen Recording permission required")

    @patch.object(desktop_app.sys, "frozen", True, create=True)
    @patch.object(desktop_app.sys, "platform", "darwin")
    def test_automatic_macos_start_only_checks_permissions(self):
        controller = desktop_app.ProjectXController()
        with patch(
            "macos_permissions.check_macos_permissions",
            return_value={"screen_recording": False, "accessibility": False},
        ) as check:
            controller._start_macos_in_process("lan")

        check.assert_called_once_with(request=False)
        self.assertEqual(controller.status, "Screen Recording permission required")


if __name__ == "__main__":
    unittest.main()
