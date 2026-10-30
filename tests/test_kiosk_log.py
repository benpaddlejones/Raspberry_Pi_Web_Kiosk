import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kiosk_wifi_setup as setup

SCRIPT = Path(__file__).resolve().parent.parent / "system" / "web-kiosk-log"


class KioskLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "journalctl").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" >> "$FAKE_JOURNAL_ARGS"\ncat "$FAKE_JOURNAL"\n',
            encoding="utf-8",
        )
        (self.bin / "journalctl").chmod(0o755)
        self.journal = self.root / "journal.txt"
        self.out = self.root / "kiosk-log.txt"

    def run_log(self, journal: str) -> str:
        self.journal.write_text(journal, encoding="utf-8")
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "KIOSK_LOG_FILE": str(self.out),
            "TMPDIR": str(self.root),
            "FAKE_JOURNAL": str(self.journal),
            "FAKE_JOURNAL_ARGS": str(self.root / "journal-args.txt"),
        }
        subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)
        return self.out.read_text(encoding="utf-8")

    def test_includes_services_that_can_block_or_fail_before_the_launcher(self):
        self.run_log("session startup\n")
        arguments = (self.root / "journal-args.txt").read_text(encoding="utf-8").splitlines()
        for service in (
            "getty@tty1.service", "systemd-logind.service",
            "NetworkManager-wait-online.service", "systemd-timesyncd.service",
        ):
            self.assertIn(service, arguments)
        self.assertIn("web-kiosk-time", arguments)

    def test_secrets_and_device_identifiers_are_masked(self):
        text = self.run_log(
            "wpa_supplicant: associated with aa:BB:cc:dd:ee:ff\n"
            "NetworkManager: Password=Hunter2 psk=abc123 token=SECRET1\n"
            "chromium: GET https://example.org/page?token=SECRET2&user=jsmith failed\n"
            "supplicant: identity=jsmith@example.org\n"
            "kiosk: Network check: open (https://example.org answered 200)\n"
        )
        for secret in ("aa:BB:cc", "Hunter2", "abc123", "SECRET1", "SECRET2", "jsmith"):
            self.assertNotIn(secret, text)
        self.assertIn("xx:xx:xx:xx:xx:xx", text)
        self.assertIn("https://example.org/***", text)
        self.assertIn("Network check: open (https://example.org answered 200)", text)

    def test_size_is_bounded_whatever_the_journal_holds(self):
        text = self.run_log("".join(f"line {index} {'x' * 5000}\n" for index in range(2000)))
        lines = text.splitlines()
        self.assertLessEqual(max(len(line) for line in lines), 500)
        self.assertLess(len(lines), 330)
        self.assertLess(len(text.encode()), 200_000)

    def test_spaced_quoted_and_json_secret_values_are_not_partly_exposed(self):
        text = self.run_log(
            'NetworkManager: password = "SYNTHETIC PRIVATE VALUE"\n'
            'NetworkManager: password="ANOTHER PRIVATE VALUE"\n'
            'supplicant: "identity": "PRIVATE USER NAME"\n'
            "supplicant: 'psk' : 'PRIVATE WIFI KEY'\n"
            'http: Authorization: Bearer PRIVATE AUTHORIZATION\n'
            'http: Cookie: session=PRIVATE SESSION\n'
        )
        for secret in ("PRIVATE", "SYNTHETIC", "ANOTHER", "Bearer", "session="):
            self.assertNotIn(secret, text)

    def test_masks_url_credentials(self):
        text = self.run_log(
            "request https://PRIVATEUSER:PRIVATEPASSWORD@example.org/page failed\n"
            "request https://PRIVATEUSER:password:PRIVATEPASSWORD@example.org/page failed\n"
            "request https://PRIVATEUSER:PRIVATE@PASSWORD@example.org/page failed\n"
        )
        self.assertNotIn("PRIVATEUSER", text)
        self.assertNotIn("PRIVATEPASSWORD", text)
        self.assertIn("example.org", text)

    def test_masks_url_path_tokens_and_fragment(self):
        text = self.run_log("request https://example.org/PRIVATEPATHTOKEN#PRIVATEFRAGMENT failed\n")
        self.assertNotIn("PRIVATE", text)
        self.assertIn("https://example.org/***", text)

    def test_masks_profile_paths_even_when_names_contain_spaces(self):
        text = self.run_log(
            'keyfile: "/etc/NetworkManager/system-connections/PRIVATE USER.nmconnection": invalid connection.type\n'
            "keyfile: '/etc/NetworkManager/system-connections/PRIVATE NAME.nmconnection': missing type\n"
        )
        self.assertNotIn("PRIVATE", text)
        self.assertIn("invalid connection.type", text)
        self.assertIn("missing type", text)

    def test_redacts_before_truncating_long_lines(self):
        text = self.run_log('x' * 480 + ' password = "PRIVATE VALUE AT THE LIMIT"\n')
        self.assertNotIn("PRIVATE", text)

    def test_each_run_replaces_the_previous_file(self):
        self.run_log("first boot problem\n")
        text = self.run_log("second boot message\n")
        self.assertNotIn("first boot problem", text)
        self.assertIn("second boot message", text)
        self.assertEqual(text.count("--- kiosk session"), 1)

    def test_lifecycle_events_are_not_crowded_out_by_verbose_graphics_logs(self):
        text = self.run_log(
            "Wi-Fi event=reset-confirmed\nWi-Fi event=reset-complete count=2\n"
            + "".join(f"graphics extension {index}\n" for index in range(500))
        )
        self.assertIn("--- Wi-Fi lifecycle events", text)
        self.assertIn("event=reset-confirmed", text)
        self.assertIn("event=reset-complete count=2", text)

    def test_setup_events_reach_the_export_without_ui_or_profile_content(self):
        with patch.object(setup, "_whiptail"), patch.object(setup, "message"), \
                patch.object(setup, "reset_wifi_profiles", return_value=2), \
                contextlib.redirect_stderr(io.StringIO()) as events:
            self.assertTrue(setup.reset_saved_wifi())
        text = self.run_log(events.getvalue())
        for event in ("reset-requested", "reset-confirmed", "reset-dialog-complete count=2"):
            self.assertIn(f"event={event}", text)
        self.assertNotIn("Remove all saved", text)


if __name__ == "__main__":
    unittest.main()
