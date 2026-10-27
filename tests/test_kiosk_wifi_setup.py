import unittest
from unittest.mock import MagicMock, patch

import kiosk_wifi_setup as setup
from kiosk_wifi_setup import Cancelled, Network


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
        results = [
            MagicMock(returncode=returncode, stderr=stderr),
            MagicMock(stdout=connectivity),
        ]
        with patch.object(setup, "_whiptail"), \
                patch.object(setup.subprocess, "run", side_effect=results) as run:
            outcome = setup.activate("School")
        command = run.call_args_list[0]
        self.assertEqual(command.kwargs["stdin"], setup.subprocess.DEVNULL)
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


class MainTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
