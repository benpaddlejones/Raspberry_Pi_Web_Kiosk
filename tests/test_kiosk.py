import contextlib
import io
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiosk import (
    ConfigError, DEFAULT_CONFIG, Probe, browser_command, main, portal_configured, probe_address,
    probe_network, read_url, wait_for_login_page, wait_for_network,
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
    @patch("kiosk.portal_configured", return_value=True)
    @patch("kiosk.wait_for_login_page")
    @patch("kiosk.wait_for_network")
    def test_launches_chromium_with_private_profile(self, wait, portal, configured, get_user_id, which, execute):
        with patch("kiosk.Path.home", return_value=self.home):
            self.assertEqual(main(["--config", str(self.config)]), 0)
        wait.assert_called_once_with("https://example.com")
        portal.assert_called_once_with("https://example.com", until_reachable=True)
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


SITE = "https://example.com/app"
PLAIN = "http://example.com/app"


class LoginPageTests(unittest.TestCase):
    def probe(self, *results, url=SITE):
        """Probe with each result in turn: an int is a response status, anything else is raised."""
        outcomes = list(results)

        def open_url(target, timeout):
            outcome = outcomes.pop(0)
            if isinstance(outcome, int):
                response = MagicMock(status=outcome)
                response.__enter__.return_value = response
                return response
            raise outcome

        opener = MagicMock()
        opener.open.side_effect = open_url
        with patch("kiosk.urllib.request.build_opener", return_value=opener):
            return probe_network(url), [call.args[0] for call in opener.open.call_args_list]

    @staticmethod
    def error(code, location=None):
        headers = {"Location": location} if location else {}
        return urllib.error.HTTPError("http://probe", code, "Status", headers, None)

    def test_probe_address_is_the_plain_http_kiosk_site(self):
        cases = {
            "https://example.com/app?x=1#top": "http://example.com/app",
            "https://example.com": "http://example.com/",
            "https://example.com:8443/x": "http://example.com/x",
            "http://192.168.1.20:8080/dashboard": "http://192.168.1.20:8080/dashboard",
            "https://[::1]/a": "http://[::1]/a",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(probe_address(url), expected)

    def test_only_200_from_the_kiosk_page_itself_is_signed_in(self):
        result, requested = self.probe(self.error(301, SITE), 200, 200)
        self.assertEqual(result, Probe("open"))
        self.assertEqual(requested, [PLAIN, SITE, SITE])

    def test_plain_http_is_tried_first_and_a_login_redirect_ends_the_probe(self):
        portal = "https://portal.example.net:6082/php/uid.php?token=abc"
        result, requested = self.probe(self.error(302, portal))
        self.assertEqual(result, Probe("portal", portal))
        self.assertEqual(requested, [PLAIN])

    def test_anything_other_than_200_is_not_signed_in(self):
        for outcome in (
            403, 404, 204, 503, self.error(403), self.error(404), self.error(302),
            urllib.error.URLError("refused"), ssl.SSLError("reset"), TimeoutError(),
        ):
            with self.subTest(outcome=repr(outcome)):
                result, requested = self.probe(outcome, outcome)
                self.assertEqual(result, Probe("blocked"))
                self.assertEqual(requested, [PLAIN, SITE])

    def test_http_upgrade_to_a_refused_https_site_is_not_signed_in(self):
        refused = urllib.error.URLError("refused")
        result, requested = self.probe(self.error(301, SITE), refused, refused)
        self.assertEqual(result, Probe("blocked"))
        self.assertEqual(requested, [PLAIN, SITE, SITE])

    def test_the_real_kiosk_url_decides_not_the_probe_address(self):
        url = SITE + "?view=1"
        result, requested = self.probe(self.error(301, SITE), 200, 200, url=url)
        self.assertEqual(result, Probe("open"))
        self.assertEqual(requested, [PLAIN, SITE, url])

    def test_http_kiosk_url_is_requested_once_as_given(self):
        url = "http://192.168.1.20:8080/dash?x=1"
        self.assertEqual(self.probe(200, url=url), (Probe("open"), [url]))
        portal = "https://portal.example.net/login"
        self.assertEqual(
            self.probe(self.error(302, portal), url=url), (Probe("portal", portal), [url])
        )

    def test_redirects_within_the_site_are_followed(self):
        for location in ("https://example.com/app/", "https://WWW.example.com/app", "/app/"):
            with self.subTest(location=location):
                result, requested = self.probe(self.error(301, location), 200, 200)
                self.assertEqual(result, Probe("open"))
                self.assertEqual(len(requested), 3)
        result, _ = self.probe(
            self.error(301, "https://example.com/"), 200, 200, url="https://www.example.com/"
        )
        self.assertEqual(result, Probe("open"))

    def test_endless_redirects_are_not_signed_in(self):
        loop = [self.error(301, "/again") for _ in range(20)]
        self.assertEqual(self.probe(*loop)[0], Probe("blocked"))

    def test_malformed_redirect_is_treated_as_a_login_page(self):
        result, _ = self.probe(self.error(302, "https://[::1/"))
        self.assertEqual(result, Probe("portal", "https://[::1/"))

    @patch("kiosk.time.sleep")
    @patch("kiosk.probe_network", side_effect=[Probe("portal"), Probe("portal"), Probe("open")])
    def test_waits_while_redirected_to_a_login_page(self, probe, pause):
        with contextlib.redirect_stderr(io.StringIO()):
            wait_for_login_page(SITE)
        self.assertEqual(probe.call_count, 3)
        probe.assert_called_with(SITE)
        self.assertEqual(pause.call_count, 2)

    @patch("kiosk.time.sleep")
    @patch("kiosk.probe_network", return_value=Probe("blocked"))
    def test_does_not_wait_for_a_blocked_site_unless_a_portal_is_configured(self, probe, pause):
        wait_for_login_page(SITE)
        pause.assert_not_called()

    @patch("kiosk.time.sleep")
    @patch("kiosk.probe_network", side_effect=[Probe("blocked"), Probe("blocked"), Probe("open")])
    def test_waits_for_the_site_to_answer_when_a_portal_is_configured(self, probe, pause):
        with contextlib.redirect_stderr(io.StringIO()):
            wait_for_login_page(SITE, until_reachable=True)
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(pause.call_count, 2)

    @patch("kiosk.time.sleep")
    @patch("kiosk.time.monotonic", side_effect=[0, 10, 200])
    @patch("kiosk.probe_network", return_value=Probe("blocked"))
    def test_gives_up_so_the_browser_can_open(self, probe, clock, pause):
        with contextlib.redirect_stderr(io.StringIO()):
            wait_for_login_page(SITE, limit=180, until_reachable=True)
        self.assertEqual(pause.call_count, 1)

    def test_portal_configured_follows_kiosk_portal_txt(self):
        from kiosk_portal import PortalError

        with patch("kiosk_portal.read_portal", return_value="portal.example.org"):
            self.assertTrue(portal_configured())
        with patch("kiosk_portal.read_portal", return_value=None):
            self.assertFalse(portal_configured())
        with patch("kiosk_portal.read_portal", side_effect=PortalError("bad")):
            self.assertFalse(portal_configured())


if __name__ == "__main__":
    unittest.main()