import configparser
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiosk_time import DEFAULT_SERVERS, TimeConfigError, main, read_servers, write_config


class TimeConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "kiosk-time.txt"

    def test_shipped_defaults_use_microsoft(self):
        project = Path(__file__).resolve().parents[1]
        self.assertEqual(DEFAULT_SERVERS, ("time.windows.com",))
        self.assertEqual(read_servers(project / "kiosk-time.txt"), DEFAULT_SERVERS)
        installed = configparser.ConfigParser(strict=False)
        installed.read(project / "system/timesyncd.conf")
        self.assertEqual(installed["Time"]["NTP"], "time.windows.com")
        self.assertEqual(installed["Time"]["FallbackNTP"], "")

    def test_reads_windows_file_with_comments_and_duplicate_servers(self):
        self.config.write_bytes(
            b"\xef\xbb\xbf# Network time\r\n\r\n time.windows.com \r\n"
            b"time.windows.com\r\n192.0.2.1 # local server\r\n2001:db8::1\r\n"
        )
        self.assertEqual(read_servers(self.config), ("time.windows.com", "192.0.2.1", "2001:db8::1"))

    def test_missing_or_blank_file_restores_defaults(self):
        self.assertEqual(read_servers(self.config), DEFAULT_SERVERS)
        self.config.write_text("\n# no custom servers\n", encoding="utf-8")
        self.assertEqual(read_servers(self.config), DEFAULT_SERVERS)

    def test_accepts_provider_and_local_hostnames(self):
        for server in ("time.apple.com", "time.windows.com", "time.google.com", "ntp", "ntp.example.org."):
            with self.subTest(server=server):
                self.config.write_text(server, encoding="utf-8")
                self.assertEqual(read_servers(self.config), (server,))

    def test_rejects_urls_ports_and_configuration_injection(self):
        for server in (
            "https://time.apple.com", "time.windows.com:123", "ntp other", "NTP=evil",
            "[Time]", "-bad.example", "bad_.example", "bad..example", ".", "bad\x00host",
            "a" * 64 + ".example", ".".join(["a" * 63] * 4),
        ):
            with self.subTest(server=server):
                self.config.write_text(server, encoding="utf-8")
                with self.assertRaises(TimeConfigError):
                    read_servers(self.config)

    def test_reports_unreadable_or_non_utf8_file(self):
        self.config.write_bytes(b"\xff")
        with self.assertRaises(TimeConfigError):
            read_servers(self.config)
        with self.assertRaises(TimeConfigError):
            read_servers(self.root)

    def test_generated_override_clears_previous_providers_and_fallbacks(self):
        output = self.root / "timesyncd.conf.d/zzz-web-kiosk.conf"
        write_config(("time.apple.com",), output)
        self.assertEqual(output.read_text(encoding="utf-8"), "[Time]\nNTP=\nNTP=time.apple.com\nFallbackNTP=\n")
        write_config(DEFAULT_SERVERS, output)
        self.assertNotIn("time.apple.com", output.read_text(encoding="utf-8"))

    @patch("kiosk_time.write_config")
    def test_check_does_not_change_system_configuration(self, write):
        self.config.write_text("time.apple.com", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config), "--check"]), 0)
        write.assert_not_called()

    @patch("kiosk_time.write_config")
    def test_main_applies_the_selected_servers(self, write):
        self.config.write_text("time.windows.com", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config)]), 0)
        write.assert_called_once_with(("time.windows.com",))

    @patch("kiosk_time.write_config")
    def test_invalid_configuration_is_reported_without_writing(self, write):
        self.config.write_text("[Time]", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertEqual(main(["--config", str(self.config)]), 1)
        self.assertIn("line 1", log.getvalue())
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()