import contextlib
import configparser
import io
import os
import pty
import select
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import kiosk_wifi_setup as setup
from kiosk_wifi_setup import Cancelled, Network
from kiosk_wifi import ConfigError, Connection


class TerminalOutputTests(unittest.TestCase):
    def test_ui_output_is_separate_from_logs_and_captured_answers(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        terminal_path = os.ttyname(slave)
        original_open = open

        def terminal_open(path, *arguments, **keywords):
            self.assertEqual(path, "/dev/tty")
            return original_open(terminal_path, *arguments, **keywords)

        command = [sys.executable, "-c", "import sys; print('VISIBLE UI'); print('PRIVATE ANSWER', file=sys.stderr)"]
        with patch("builtins.open", side_effect=terminal_open), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            setup.wifi_event("setup-opened")
            result = setup._run_ui(command, capture_answer=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "PRIVATE ANSWER")
        self.assertTrue(select.select([master], [], [], 2)[0])
        screen = os.read(master, 4096).decode()
        self.assertIn("VISIBLE UI", screen)
        self.assertNotIn("PRIVATE ANSWER", screen)
        self.assertNotIn("Wi-Fi event", screen)
        self.assertIn("event=setup-opened", log.getvalue())
        self.assertNotIn("PRIVATE ANSWER", log.getvalue())

    def test_only_ui_programs_receive_the_terminal(self):
        with patch("builtins.open") as terminal_open, patch.object(setup.subprocess, "run") as run:
            terminal = terminal_open.return_value.__enter__.return_value
            setup._run_ui(["nmtui"])
        self.assertIs(run.call_args.kwargs["stdout"], terminal)
        self.assertIs(run.call_args.kwargs["stderr"], terminal)
        self.assertIs(run.call_args.kwargs["stdin"], sys.stdin)

    def test_whiptail_answers_remain_captured(self):
        with patch.object(setup, "_run_ui", return_value=MagicMock(returncode=0, stderr="answer")) as run:
            self.assertEqual(setup._whiptail("--inputbox", "Question", "10", "76"), "answer")
        self.assertTrue(run.call_args.kwargs["capture_answer"])

    def test_service_routes_background_output_to_journal(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(Path(__file__).resolve().parents[1] / "system/web-kiosk-network-setup.service")
        self.assertEqual(unit["Service"]["StandardOutput"], "journal")
        self.assertEqual(unit["Service"]["StandardError"], "journal")
        self.assertEqual(unit["Service"]["StandardInput"], "tty")


class ScanTests(unittest.TestCase):
    def test_split_terse_handles_escaped_colons(self):
        self.assertEqual(setup.split_terse(r"Cafe\:Guest:WPA2:70"), ["Cafe:Guest", "WPA2", "70"])

    def test_security_from_scan(self):
        cases = {
            "WPA2 802.1X": "wpa-enterprise",
            "WPA1 WPA2": "wpa-personal",
            "WPA2 WPA3": "wpa-personal",
            "WPA3": "wpa3-personal",
            "OWE": "owe",
            "WEP": "wep",
            "": "none",
        }
        for flags, expected in cases.items():
            with self.subTest(flags=flags):
                self.assertEqual(setup.security_from_scan(flags), expected)

    def test_parse_scan_deduplicates_and_sorts_by_signal(self):
        output = "School:WPA2 802.1X:40\nHome:WPA2:80\nSchool:WPA2 802.1X:65\n:WPA2:90\n"
        self.assertEqual(setup.parse_scan(output), [
            Network("Home", "wpa-personal", 80),
            Network("School", "wpa-enterprise", 65),
        ])


class CollectSettingsTests(unittest.TestCase):
    def test_personal_asks_only_for_password(self):
        with patch.object(setup, "ask", return_value="pass word") as ask:
            values = setup.collect_settings("Home", "wpa-personal", False)
        self.assertEqual(values, {"ssid": "Home", "security": "wpa-personal", "password": "pass word"})
        ask.assert_called_once()

    def test_enterprise_defaults_to_peap_without_certificate(self):
        with patch.object(setup, "ask", side_effect=["jsmith", "secret"]), \
                patch.object(setup, "menu", return_value="none"):
            values = setup.collect_settings("School", "wpa-enterprise", False)
        self.assertEqual(values, {
            "ssid": "School", "security": "wpa-enterprise", "eap": "peap",
            "username": "jsmith", "password": "secret", "ca-cert": "none",
        })

    def test_enterprise_with_system_certificates_and_domain(self):
        with patch.object(setup, "ask", side_effect=["jsmith", "secret"]), \
                patch.object(setup, "menu", return_value="system"), \
                patch.object(setup, "_whiptail", return_value=" radius.example.org "):
            values = setup.collect_settings("School", "wpa-enterprise", True)
        self.assertEqual(values["ca-cert"], "system")
        self.assertEqual(values["domain"], "radius.example.org")
        self.assertEqual(values["hidden"], "yes")


class ActivateTests(unittest.TestCase):
    def run_activate(self, returncode, stderr="", connectivity="full"):
        with patch.object(setup, "_whiptail"), \
                patch.object(setup, "save_connection", side_effect=ConfigError(stderr) if returncode else None) as save, \
                patch.object(setup.subprocess, "run", return_value=MagicMock(stdout=connectivity)):
            connection = Connection("profile", [], [])
            outcome = setup.activate("School", connection)
        save.assert_called_once_with(connection)
        return outcome

    def test_success_and_portal(self):
        self.assertEqual(self.run_activate(0)[0], True)
        connected, text = self.run_activate(0, connectivity="portal")
        self.assertTrue(connected)
        self.assertIn("web login", text)

    def test_failure_messages(self):
        connected, text = self.run_activate(4, "Error: Secrets were required, but not provided.")
        self.assertFalse(connected)
        self.assertIn("username or password", text)
        self.assertIn("could not be found", self.run_activate(10, "No network with SSID 'School' found.")[1])

    def test_connectivity_timeout_does_not_undo_successful_wifi_connection(self):
        with patch.object(setup, "_whiptail"), patch.object(setup, "save_connection") as save, \
                patch.object(setup.subprocess, "run", side_effect=subprocess.TimeoutExpired("nmcli", 15)):
            self.assertTrue(setup.activate("School", Connection("profile", [], []))[0])
        save.assert_called_once()


class ConnectTests(unittest.TestCase):
    def test_cancelling_credentials_does_not_change_profiles(self):
        with patch.object(setup, "choose_network", return_value=("School", "wpa-enterprise", False)), \
                patch.object(setup, "collect_settings", side_effect=Cancelled), \
                patch.object(setup, "save_connection") as save, \
                contextlib.redirect_stderr(io.StringIO()) as log:
            with self.assertRaises(Cancelled):
                setup.connect_wifi()
        save.assert_not_called()
        self.assertIn("event=setup-connect-cancelled", log.getvalue())
        self.assertNotIn("School", log.getvalue())


class ResetTests(unittest.TestCase):
    def test_cancelled_confirmation_changes_nothing(self):
        with patch.object(setup, "_whiptail", side_effect=Cancelled), \
                patch.object(setup, "reset_wifi_profiles") as reset, \
                contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertFalse(setup.reset_saved_wifi())
        reset.assert_not_called()
        self.assertIn("event=reset-requested", log.getvalue())
        self.assertIn("event=reset-cancelled", log.getvalue())
        self.assertNotIn("event=reset-confirmed", log.getvalue())

    def test_reset_requires_explicit_confirmation(self):
        with patch.object(setup, "_whiptail") as dialog, \
                patch.object(setup, "reset_wifi_profiles", return_value=2) as reset, \
                patch.object(setup, "message") as message, \
                contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertTrue(setup.reset_saved_wifi())
        self.assertIn("--defaultno", dialog.call_args.args)
        reset.assert_called_once_with()
        self.assertIn("Removed 2", message.call_args.args[0])
        self.assertIn("event=reset-confirmed", log.getvalue())
        self.assertIn("event=reset-dialog-complete count=2", log.getvalue())

    def test_reset_failure_is_shown(self):
        with patch.object(setup, "_whiptail"), \
                patch.object(setup, "reset_wifi_profiles", side_effect=ConfigError("unavailable")), \
                patch.object(setup, "message") as message:
            self.assertFalse(setup.reset_saved_wifi())
        self.assertIn("could not finish", message.call_args.args[0])

    def test_raw_reset_errors_are_not_logged(self):
        with patch.object(setup, "_whiptail"), \
                patch.object(setup, "reset_wifi_profiles", side_effect=ConfigError("PRIVATE RAW DETAIL")), \
                patch.object(setup, "message"), contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertFalse(setup.reset_saved_wifi())
        self.assertIn("event=reset-dialog-failed", log.getvalue())
        self.assertNotIn("PRIVATE", log.getvalue())


class MainTests(unittest.TestCase):
    def setUp(self):
        self.check_saved = self.enterContext(patch.object(setup, "check_saved_wifi"))

    @patch.object(setup, "status", return_value="Not connected.")
    def test_cancel_and_finish_exit(self, status):
        with patch.object(setup, "menu", side_effect=Cancelled):
            self.assertEqual(setup.main(), 0)
        with patch.object(setup, "menu", return_value="done"):
            self.assertEqual(setup.main(), 0)

    @patch.object(setup, "status", return_value="Not connected.")
    def test_back_from_wifi_returns_to_menu(self, status):
        with patch.object(setup, "menu", side_effect=["wifi", "done"]), \
                patch.object(setup, "connect_wifi", side_effect=Cancelled) as connect:
            self.assertEqual(setup.main(), 0)
        connect.assert_called_once()

    @patch.object(setup, "status", return_value="Not connected.")
    def test_entering_setup_never_resets_profiles(self, status):
        with patch.object(setup, "menu", return_value="done"), \
                patch.object(setup, "reset_wifi_profiles") as reset:
            self.assertEqual(setup.main(), 0)
        reset.assert_not_called()
        self.check_saved.assert_called_once_with()

    @patch.object(setup, "status", return_value="Not connected.")
    def test_reset_menu_routes_through_confirmation(self, status):
        with patch.object(setup, "menu", side_effect=["reset", "done"]), \
                patch.object(setup, "reset_saved_wifi") as reset:
            self.assertEqual(setup.main(), 0)
        reset.assert_called_once_with()

    @patch.object(setup, "status", return_value="Not connected.")
    def test_advanced_settings_outcomes_are_logged(self, status):
        with patch.object(setup, "menu", side_effect=["advanced", "done"]), \
                patch.object(setup, "_run_ui", return_value=MagicMock(returncode=0)), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertEqual(setup.main(), 0)
        for event in ("setup-opened", "advanced-opened", "advanced-closed code=0", "setup-finished"):
            self.assertIn(f"event={event}", log.getvalue())


if __name__ == "__main__":
    unittest.main()
