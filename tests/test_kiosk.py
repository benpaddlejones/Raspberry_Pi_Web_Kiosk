import contextlib
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiosk import (
    ConfigError, DEFAULT_CONFIG, behind_login_page, browser_command, main, read_url,
    wait_for_login_page, wait_for_network,
)


class ReadUrlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "kiosk-url.txt"

    def test_default_is_on_readable_boot_partition(self):
        self.assertEqual(DEFAULT_CONFIG, Path("/boot/firmware/kiosk-url.txt"))

    def test_reads_windows_notepad_file(self):
        self.config.write_bytes(b"\xef\xbb\xbfhttps://example.com/dashboard\r\n")
        self.assertEqual(read_url(self.config), "https://example.com/dashboard")

    def test_accepts_blank_lines_and_surrounding_spaces(self):
        self.config.write_text("\n  https://example.com  \n\n", encoding="utf-8")
        self.assertEqual(read_url(self.config), "https://example.com")

    def test_accepts_http_and_url_components(self):
        url = "http://192.168.1.20:8080/dashboard?view=main&refresh=1#status"
        self.config.write_text(url, encoding="utf-8")
        self.assertEqual(read_url(self.config), url)

    def test_rejects_invalid_configuration(self):
        invalid_values = (
            "",
            "https://example.com\nhttps://other.example.com",
            "example.com",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "--no-sandbox",
            "https://",
            "https://example.com:invalid",
            "https://example.com:70000",
            "https://example.com:0",
            "https://[invalid",
            "https://example.com/a b",
            "https://example.com/\x00",
            "https://example.com\\path",
            "https://user:password@example.com",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.config.write_text(value, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    read_url(self.config)

    def test_rejects_missing_file(self):
        with self.assertRaises(ConfigError):
            read_url(self.config)

    def test_rejects_non_utf8_file(self):
        self.config.write_bytes(b"\xff\xfehttps://example.com")
        with self.assertRaises(ConfigError):
            read_url(self.config)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.config = self.home / "kiosk-url.txt"
        self.config.write_text("https://example.com", encoding="utf-8")
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_url_is_a_single_argument_and_sandbox_is_not_disabled(self):
        url = "https://example.com/?value=$(touch${IFS}/tmp/unwanted)&other=1"
        command = browser_command("/usr/bin/chromium", url, self.home)
        self.assertEqual(command[-1], url)
        self.assertIn("--kiosk", command)
        self.assertIn("--ozone-platform=wayland", command)
        self.assertNotIn("--no-sandbox", command)
        self.assertNotIn("--ignore-certificate-errors", command)

    @patch("kiosk.wait_for_network")
    @patch("kiosk.os.execv")
    def test_check_does_not_launch_browser(self, execute, wait):
        self.assertEqual(main(["--config", str(self.config), "--check"]), 0)
        execute.assert_not_called()
        wait.assert_not_called()

    @patch("kiosk.os.execv")
    @patch("kiosk.os.geteuid", return_value=0)
    def test_refuses_root(self, get_user_id, execute):
        self.assertEqual(main(["--config", str(self.config)]), 1)
        execute.assert_not_called()

    @patch("kiosk.os.execv")
    @patch("kiosk.shutil.which", return_value=None)
    @patch("kiosk.os.geteuid", return_value=1000)
    def test_missing_browser_is_reported(self, get_user_id, which, execute):
        self.assertEqual(main(["--config", str(self.config)]), 1)
        execute.assert_not_called()

    @patch("kiosk.os.execv")
    @patch("kiosk.shutil.which", return_value="/usr/bin/chromium")
    @patch("kiosk.os.geteuid", return_value=1000)
    @patch("kiosk.wait_for_login_page")
    @patch("kiosk.wait_for_network")
    def test_launches_chromium_with_private_profile(self, wait, portal, get_user_id, which, execute):
        with patch("kiosk.Path.home", return_value=self.home):
            self.assertEqual(main(["--config", str(self.config)]), 0)
        wait.assert_called_once_with("https://example.com")
        portal.assert_called_once_with()
        profile = self.home / ".local/share/raspberry-pi-web-kiosk/chromium"
        self.assertTrue(profile.is_dir())
        self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
        execute.assert_called_once_with(
            "/usr/bin/chromium",
            browser_command("/usr/bin/chromium", "https://example.com", profile),
        )


class NetworkTests(unittest.TestCase):
    @patch("kiosk.time.sleep")
    @patch("kiosk.socket.create_connection")
    def test_waits_for_host_to_become_reachable(self, connect, pause):
        connect.side_effect = [OSError("network unavailable"), MagicMock()]
        with contextlib.redirect_stderr(io.StringIO()):
            wait_for_network("https://example.com/dashboard")
        self.assertEqual(connect.call_count, 2)
        connect.assert_called_with(("example.com", 443), timeout=5)
        pause.assert_called_once_with(5)

    @patch("kiosk.time.sleep")
    @patch("kiosk.socket.create_connection")
    def test_uses_configured_port_without_waiting_when_online(self, connect, pause):
        wait_for_network("http://192.168.1.20:8080/dashboard")
        connect.assert_called_once_with(("192.168.1.20", 8080), timeout=5)
        pause.assert_not_called()


class LoginPageTests(unittest.TestCase):
    def probe(self, response=None, error=None):
        opener = MagicMock()
        if error:
            opener.open.side_effect = error
        else:
            opener.open.return_value.__enter__.return_value = MagicMock(status=response)
        with patch("kiosk.urllib.request.build_opener", return_value=opener):
            return behind_login_page()

    def test_detects_redirect_and_intercepted_page(self):
        redirect = urllib.error.HTTPError("http://probe", 302, "Found", {}, None)
        self.assertTrue(self.probe(error=redirect))
        self.assertTrue(self.probe(response=200))

    def test_open_internet_blocked_or_unreachable_probe_does_not_wait(self):
        self.assertFalse(self.probe(response=204))
        blocked = urllib.error.HTTPError("http://probe", 403, "Forbidden", {}, None)
        self.assertFalse(self.probe(error=blocked))
        self.assertFalse(self.probe(error=urllib.error.URLError("unreachable")))

    @patch("kiosk.time.sleep")
    @patch("kiosk.behind_login_page", side_effect=[True, True, False])
    def test_waits_until_login_page_clears(self, portal, pause):
        with contextlib.redirect_stderr(io.StringIO()):
            wait_for_login_page()
        self.assertEqual(portal.call_count, 3)
        self.assertEqual(pause.call_count, 2)

    @patch("kiosk.time.sleep")
    @patch("kiosk.time.monotonic", side_effect=[0, 10, 200])
    @patch("kiosk.behind_login_page", return_value=True)
    def test_gives_up_so_the_login_page_can_be_used(self, portal, clock, pause):
        with contextlib.redirect_stderr(io.StringIO()):
            wait_for_login_page(limit=180)
        self.assertEqual(pause.call_count, 1)


if __name__ == "__main__":
    unittest.main()