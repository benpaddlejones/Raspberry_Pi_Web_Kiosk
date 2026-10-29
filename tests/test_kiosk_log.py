import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        self.assertIn("https://example.org/page?***", text)
        self.assertIn("Network check: open (https://example.org answered 200)", text)

    def test_size_is_bounded_whatever_the_journal_holds(self):
        text = self.run_log("".join(f"line {index} {'x' * 5000}\n" for index in range(2000)))
        lines = text.splitlines()
        self.assertLessEqual(max(len(line) for line in lines), 500)
        self.assertLess(len(lines), 330)
        self.assertLess(len(text.encode()), 200_000)

    def test_each_run_replaces_the_previous_file(self):
        self.run_log("first boot problem\n")
        text = self.run_log("second boot message\n")
        self.assertNotIn("first boot problem", text)
        self.assertIn("second boot message", text)
        self.assertEqual(text.count("--- kiosk session"), 1)


if __name__ == "__main__":
    unittest.main()
