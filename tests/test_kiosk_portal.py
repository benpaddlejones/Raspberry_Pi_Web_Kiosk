import contextlib
import http.client
import io
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import kiosk_portal as portal
from kiosk import Probe
from kiosk_portal import Credentials, PortalError

HOST = "portal.example.org"
LOCATION = f"https://{HOST}:6082/php/uid.php?vsys=1&token=abc"
SECRET = "S3cret-pass!&=%"
SITE = "https://tempehs.example.org/timetable/"
LOGIN_PAGE = (
    '<input type="password" id="passwd" name="passwd">'
    '<script>var respStatus = ""; thisForm.preauthid.value = "68e5f3dc003d5965";</script>'
)
CREDENTIALS = Credentials("DET\\jsmith", SECRET)


class PortalTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.boot = Path(directory.name)
        self.config = self.boot / "kiosk-portal.txt"
        self.config.write_text(f"portal = {HOST}\n", encoding="utf-8")
        (self.boot / "kiosk-url.txt").write_text(SITE, encoding="utf-8")
        self.state = self.boot / "state" / "portal-state.json"


class ReadPortalTests(PortalTestCase):
    def test_shipped_template_is_off_until_a_portal_is_set(self):
        template = Path(__file__).resolve().parent.parent / "kiosk-portal.txt"
        self.assertIsNone(portal.read_portal(template))

    def test_missing_blank_and_commented_out_mean_off(self):
        self.assertIsNone(portal.read_portal(self.boot / "missing.txt"))
        for text in ("", f"# portal = {HOST}\n", "portal =\n"):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                self.assertIsNone(portal.read_portal(self.config))

    def test_accepts_windows_text_and_any_case(self):
        self.config.write_bytes(f"\xef\xbb\xbfPortal = {HOST.upper()}\r\n".encode("latin-1"))
        self.assertEqual(portal.read_portal(self.config), HOST)

    def test_rejects_invalid_settings(self):
        for text in (
            "portal = evil.example/path\n", "portal = host.example:6082\n", "portal = localhost\n",
            "portal = a.example\nportal = b.example\n", "other = x\n", "just words\n",
            f"portal = https://{HOST}\n",
        ):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                with self.assertRaises(PortalError):
                    portal.read_portal(self.config)


class LoginPageTests(unittest.TestCase):
    def test_reads_preauth_id(self):
        self.assertEqual(portal.parse_login_page(LOGIN_PAGE), "68e5f3dc003d5965")

    def test_missing_preauth_id_is_empty(self):
        self.assertEqual(portal.parse_login_page('<input id="passwd">'), "")

    def test_unrecognised_and_challenge_pages_are_refused(self):
        for body in ("<html>Welcome</html>", LOGIN_PAGE.replace('respStatus = ""', 'respStatus = "Challenge"')):
            with self.subTest(body=body), self.assertRaises(PortalError):
                portal.parse_login_page(body)

    def test_form_matches_the_portal_script(self):
        form = portal.login_form(CREDENTIALS, "abc")
        self.assertEqual(form["user"], "DET\\jsmith")
        self.assertEqual(form["escapeUser"], "DET\\\\jsmith")
        self.assertEqual(form["passwd"], SECRET)
        self.assertEqual(
            sorted(form), ["escapeUser", "inputStr", "ok", "passwd", "preauthid", "user"]
        )


class NetworkTests(unittest.TestCase):
    def test_untrusted_certificate_is_reported_without_sending(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError("bad"))
        with self.assertRaisesRegex(PortalError, "certificate is not trusted"):
            portal.fetch(opener, LOCATION, {"passwd": SECRET})

    def test_timeouts_are_reported(self):
        opener = MagicMock()
        opener.open.side_effect = TimeoutError()
        with self.assertRaisesRegex(PortalError, "Could not reach"):
            portal.fetch(opener, LOCATION)

    def test_garbled_responses_are_reported_not_raised(self):
        opener = MagicMock()
        opener.open.side_effect = http.client.BadStatusLine("junk")
        with self.assertRaisesRegex(PortalError, "Could not reach"):
            portal.fetch(opener, LOCATION)

    def test_http_errors_return_their_page(self):
        opener = MagicMock()
        error = urllib.error.HTTPError(LOCATION, 302, "Found", {}, io.BytesIO(b"page"))
        opener.open.side_effect = error
        self.assertEqual(portal.fetch(opener, LOCATION), (302, "page"))


class CredentialTests(unittest.TestCase):
    def read(self, outputs):
        with patch.object(portal, "_nmcli", side_effect=outputs) as nmcli:
            return portal.read_credentials(), nmcli

    def test_reads_saved_wifi_credentials(self):
        credentials, nmcli = self.read(["uuid-1:802-11-wireless\nuuid-2:ethernet\n", "DET\\jsmith\n", f"{SECRET}\n"])
        self.assertEqual(credentials, Credentials("DET\\jsmith", SECRET))
        self.assertIn("--show-secrets", nmcli.call_args_list[2].args)
        self.assertIn("--escape", nmcli.call_args_list[1].args)

    def test_requires_wifi_with_username_and_password(self):
        for outputs in (["uuid-2:ethernet\n"], ["uuid-1:802-11-wireless\n", "", ""], ["uuid-1:802-11-wireless\n", "user\n", ""]):
            with self.subTest(outputs=outputs), self.assertRaises(PortalError):
                self.read(outputs)


class StateTests(PortalTestCase):
    def test_corrupt_or_wrong_type_state_gives_defaults(self):
        self.state.parent.mkdir()
        for content in ("{broken", "[]", '{"failures": "many", "last_failure": null}'):
            with self.subTest(content=content):
                self.state.write_text(content, encoding="utf-8")
                state = portal.load_state(self.state)
                self.assertEqual((state["failures"], state["last_failure"]), (0, 0.0))
                self.assertTrue(state["salt"])

    def test_state_is_private_and_never_holds_the_password(self):
        state = portal.load_state(self.state)
        state["fingerprint"] = portal.fingerprint(state, CREDENTIALS)
        portal.save_state(self.state, state)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(SECRET, self.state.read_text(encoding="utf-8"))


class CheckOnceTests(PortalTestCase):
    def setUp(self):
        super().setUp()
        patches = {
            "probe_network": patch.object(portal, "probe_network"),
            "clock_is_synchronized": patch.object(portal, "clock_is_synchronized", return_value=True),
            "read_credentials": patch.object(portal, "read_credentials", return_value=CREDENTIALS),
            "fetch": patch.object(portal, "fetch", return_value=(200, LOGIN_PAGE)),
            "sleep": patch.object(portal.time, "sleep"),
        }
        for name, item in patches.items():
            setattr(self, name, self.enterContext(item))
        self.probe_network.return_value = Probe("portal", LOCATION)

    def check(self, now=1_000_000.0):
        return portal.check_once(self.config, self.state, now)

    def posts(self):
        return [call for call in self.fetch.call_args_list if len(call.args) > 2]

    def test_off_without_a_portal_setting(self):
        self.config.write_text("# portal = none\n", encoding="utf-8")
        self.assertIn("is off", self.check())
        self.probe_network.assert_not_called()

    def test_blocked_status_says_what_the_network_did(self):
        self.probe_network.return_value = Probe("blocked", detail="http://x answered 301; https://x failed (SSLError)")
        message = self.check()
        self.assertIn("(http://x answered 301; https://x failed (SSLError))", message)
        self.assertIn("nothing was sent", message)

    def test_nothing_to_do_when_the_site_answers_and_nothing_sent_when_blocked(self):
        for state, text in (("open", "no sign-in needed"), ("blocked", "nothing was sent")):
            with self.subTest(state=state):
                self.probe_network.return_value = Probe(state)
                self.assertIn(text, self.check())
        self.probe_network.assert_called_with(SITE)
        self.fetch.assert_not_called()
        self.read_credentials.assert_not_called()

    def test_https_network_check_is_deferred_until_clock_is_synchronized(self):
        self.clock_is_synchronized.return_value = False
        self.assertIn("deferring HTTPS network check", self.check())
        self.probe_network.assert_not_called()
        self.fetch.assert_not_called()

    def test_unreadable_kiosk_url_stops_before_probing(self):
        (self.boot / "kiosk-url.txt").write_text("not a url", encoding="utf-8")
        with self.assertRaisesRegex(PortalError, "kiosk-url.txt"):
            self.check()
        self.probe_network.assert_not_called()

    def test_credentials_are_never_sent_to_another_host(self):
        for location in (
            "https://evil.example/php/uid.php", f"https://{HOST}.evil.example/", f"https://evil.example/{HOST}",
            f"https://{HOST}@evil.example/", "https://[::1/",
        ):
            with self.subTest(location=location):
                self.probe_network.return_value = Probe("portal", location)
                with self.assertRaisesRegex(PortalError, "address is not valid|no credentials were sent"):
                    self.check()
        self.fetch.assert_not_called()
        self.read_credentials.assert_not_called()

    def test_credentials_are_never_sent_over_http(self):
        self.probe_network.return_value = Probe("portal", f"http://{HOST}/php/uid.php")
        with self.assertRaisesRegex(PortalError, "not HTTPS"):
            self.check()
        self.fetch.assert_not_called()

    def test_signs_in_and_confirms_internet_access(self):
        self.probe_network.side_effect = [Probe("portal", LOCATION), Probe("portal", LOCATION), Probe("open")]
        self.assertEqual(self.check(), "Signed in to the network login page.")
        (post,) = self.posts()
        self.assertEqual(post.args[1], LOCATION)
        self.assertEqual(post.args[2]["passwd"], SECRET)
        self.assertEqual(post.args[2]["preauthid"], "68e5f3dc003d5965")
        self.assertEqual(portal.load_state(self.state)["failures"], 0)

    def test_rejected_sign_in_waits_15_minutes_and_hides_the_password(self):
        message = self.check(now=1_000_000.0)
        self.assertIn("not accepted", message)
        self.assertNotIn(SECRET, message)
        self.assertEqual(len(self.posts()), 1)
        self.assertNotIn(SECRET, self.state.read_text(encoding="utf-8"))

        self.assertEqual(self.check(now=1_000_000.0 + 14 * 60), message)
        self.assertEqual(len(self.posts()), 1)

        self.check(now=1_000_000.0 + 16 * 60)
        self.assertEqual(len(self.posts()), 2)

    def test_repeated_failures_slow_to_once_a_day_until_credentials_change(self):
        now = 1_000_000.0
        for _ in range(3):
            self.check(now=now)
            now += 16 * 60
        self.assertEqual(len(self.posts()), 3)

        message = self.check(now=now)
        self.assertIn("once a day", message)
        self.assertEqual(len(self.posts()), 3)
        self.check(now=now + 23 * 3600)
        self.assertEqual(len(self.posts()), 3)
        self.check(now=now + 25 * 3600)
        self.assertEqual(len(self.posts()), 4)

        self.read_credentials.return_value = Credentials("DET\\jsmith", "new password")
        self.check(now=now + 25 * 3600 + 1)
        self.assertEqual(len(self.posts()), 5)

    def test_unsupported_login_page_is_not_submitted_or_counted(self):
        self.fetch.return_value = (200, LOGIN_PAGE.replace('respStatus = ""', 'respStatus = "Challenge"'))
        with self.assertRaisesRegex(PortalError, "extra verification"):
            self.check()
        self.assertEqual(self.posts(), [])
        self.assertEqual(portal.load_state(self.state)["failures"], 0)

    def test_failing_to_load_the_page_is_not_counted(self):
        self.fetch.side_effect = PortalError("Could not reach the login page.")
        with self.assertRaises(PortalError):
            self.check()
        self.assertFalse(self.state.exists())

    def test_open_internet_clears_earlier_failures(self):
        self.check()
        self.assertEqual(portal.load_state(self.state)["failures"], 1)
        self.probe_network.return_value = Probe("open")
        self.check(now=1_000_100.0)
        self.assertEqual(portal.load_state(self.state)["failures"], 0)

    def test_clock_set_backwards_still_waits(self):
        self.check(now=1_000_000.0)
        self.assertIn("not accepted", self.check(now=500.0))
        self.assertEqual(len(self.posts()), 1)


class RunAndCommandTests(PortalTestCase):
    def test_status_file_has_no_credentials(self):
        portal.write_status(self.config, "Signed in to the network login page.")
        status = (self.boot / "kiosk-portal-status.txt").read_bytes()
        self.assertIn(b"\r\nSigned in to the network login page.\r\n", status)
        self.assertNotIn(SECRET.encode(), status)

    def test_check_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(portal.main(["--config", str(self.config), "--check"]), 0)
        self.assertIn(HOST, output.getvalue())
        self.config.write_text("portal = nodots\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(portal.main(["--config", str(self.config), "--check"]), 1)

    def test_service_starts_only_when_a_portal_is_set(self):
        unit = (Path(__file__).resolve().parent.parent / "system" / "web-kiosk-portal.service").read_text()
        self.assertIn("ExecCondition=/usr/bin/python3 -B /usr/local/lib/raspberry-pi-web-kiosk/kiosk_portal.py --enabled", unit)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(portal.main(["--config", str(self.config), "--enabled"]), 0)

    def test_enabled_is_false_when_off_and_clears_a_stale_status(self):
        status = self.boot / "kiosk-portal-status.txt"
        for text in ("", "# portal = portal.example.org\n", "portal =\n"):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                status.write_text("old result", encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(portal.main(["--config", str(self.config), "--enabled"]), 1)
                self.assertFalse(status.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(portal.main(["--config", str(self.boot / "missing.txt"), "--enabled"]), 1)

    def test_enabled_reports_an_invalid_setting_without_starting(self):
        self.config.write_text("portal = edgeportal.<domain>\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(portal.main(["--config", str(self.config), "--enabled"]), 1)
        self.assertIn("one host name", (self.boot / "kiosk-portal-status.txt").read_text(encoding="utf-8"))

    def test_run_reports_only_changes_and_survives_unexpected_errors(self):
        class Stop(BaseException):
            pass

        results = ["a", "a", PortalError("b"), RuntimeError("boom"), Stop()]
        with patch.object(portal, "check_once", side_effect=results), \
                patch.object(portal.time, "sleep"), patch.object(portal, "write_status") as write, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Stop):
            portal.run(self.config)
        self.assertEqual(
            [call.args[1] for call in write.call_args_list],
            ["a", "b", "Unexpected error (RuntimeError); will try again."],
        )


if __name__ == "__main__":
    unittest.main()
