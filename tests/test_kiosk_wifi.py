import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiosk_wifi import (
    APPLIED_NAME, ERROR_NAME, ConfigError, apply, build_connection, main, read_settings,
)


def sections(keyfile: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in keyfile.splitlines():
        if line.startswith("["):
            current = result.setdefault(line.strip("[]"), {})
        elif "=" in line:
            key, _, value = line.partition("=")
            current[key] = value
    return result


class WifiTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.boot = self.root / "boot"
        self.boot.mkdir()
        self.config = self.boot / "kiosk-wifi.txt"
        self.certs = self.root / "certs"

    def build(self, text: str) -> dict[str, dict[str, str]]:
        self.config.write_text(text, encoding="utf-8")
        return sections(build_connection(read_settings(self.config), self.boot, self.certs).keyfile)


class ReadSettingsTests(WifiTestCase):
    def test_template_has_no_active_settings(self):
        template = Path(__file__).resolve().parent.parent / "kiosk-wifi.txt"
        self.assertEqual(read_settings(template), {})

    def test_missing_file_has_no_settings(self):
        self.assertEqual(read_settings(self.config), {})

    def test_reads_windows_notepad_file(self):
        self.config.write_bytes(b"\xef\xbb\xbfSSID = Office #2\r\npassword = a=b#c d\r\n")
        self.assertEqual(read_settings(self.config), {"ssid": "Office #2", "password": "a=b#c d"})

    def test_errors_never_echo_the_line(self):
        for text in ("SuperSecret123\n", "pasword = SuperSecret123\n"):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError) as caught:
                    read_settings(self.config)
                self.assertNotIn("SuperSecret123", str(caught.exception))

    def test_rejects_duplicates_and_control_characters(self):
        for text in ("ssid = a\nssid = b\n", "ssid = a\x01b\n"):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    read_settings(self.config)


class BuildConnectionTests(WifiTestCase):
    def test_wpa_personal_is_default(self):
        result = self.build("ssid = Home\npassword = correct horse\n")
        self.assertEqual(result["wifi"]["ssid"], "Home")
        self.assertEqual(result["wifi-security"], {"key-mgmt": "wpa-psk", "psk": "correct horse"})
        self.assertNotIn("hidden", result["wifi"])

    def test_wpa_personal_password_length(self):
        for password in ("short", "x" * 64):
            with self.subTest(password=password), self.assertRaises(ConfigError):
                self.build(f"ssid = Home\npassword = {password}\n")
        self.build("ssid = Home\npassword = " + "a1" * 32 + "\n")

    def test_open_owe_and_hidden(self):
        self.assertNotIn("wifi-security", self.build("ssid = Cafe\nsecurity = none\n"))
        result = self.build("ssid = Cafe\nsecurity = owe\nhidden = yes\n")
        self.assertEqual(result["wifi-security"], {"key-mgmt": "owe"})
        self.assertEqual(result["wifi"]["hidden"], "true")

    def test_wpa3_personal(self):
        result = self.build("ssid = Home\nsecurity = wpa3-personal\npassword = secret\n")
        self.assertEqual(result["wifi-security"]["key-mgmt"], "sae")

    def test_wep_options(self):
        result = self.build(
            "ssid = Old\nsecurity = wep\npassword = 0123456789\nwep-key-index = 3\nwep-auth = shared\n"
        )
        self.assertEqual(result["wifi-security"], {
            "key-mgmt": "none", "auth-alg": "shared", "wep-key-type": "1",
            "wep-tx-keyidx": "2", "wep-key2": "0123456789",
        })
        result = self.build("ssid = Old\nsecurity = wep\nwep-key-type = passphrase\npassword = anything\n")
        self.assertEqual(result["wifi-security"]["wep-key-type"], "2")
        with self.assertRaises(ConfigError):
            self.build("ssid = Old\nsecurity = wep\npassword = 1234\n")

    def test_leap(self):
        result = self.build("ssid = Old\nsecurity = leap\nusername = me\npassword = pw\n")
        self.assertEqual(result["wifi-security"]["leap-username"], "me")
        self.assertEqual(result["wifi-security"]["auth-alg"], "leap")

    def test_peap_without_ca_certificate(self):
        self.config.write_text(
            "ssid = Corp\nsecurity = wpa-enterprise\nusername = staff\\jo\npassword = pw\n"
            "ca-cert = none\npeap-version = 0\nanonymous-identity = anon\n",
            encoding="utf-8",
        )
        connection = build_connection(read_settings(self.config), self.boot, self.certs)
        eap = sections(connection.keyfile)["802-1x"]
        self.assertEqual(eap["eap"], "peap;")
        self.assertEqual(eap["identity"], "staff\\\\jo")
        self.assertEqual(eap["phase2-auth"], "mschapv2")
        self.assertEqual(eap["phase1-peapver"], "0")
        self.assertEqual(eap["anonymous-identity"], "anon")
        self.assertNotIn("ca-cert", eap)
        self.assertTrue(connection.warnings)

    def test_ca_certificate_choice_is_required(self):
        with self.assertRaises(ConfigError):
            self.build("ssid = Corp\nsecurity = wpa-enterprise\nusername = u\npassword = p\n")

    def test_ttls_inner_authentication_mapping(self):
        base = "ssid = Corp\nsecurity = wpa-enterprise\neap = ttls\nusername = u\npassword = p\nca-cert = system\n"
        result = self.build(base + "inner-auth = mschapv2-no-eap\ndomain = example.org\n")
        self.assertEqual(result["802-1x"]["phase2-auth"], "mschapv2")
        self.assertEqual(result["802-1x"]["system-ca-certs"], "true")
        self.assertEqual(result["802-1x"]["domain-suffix-match"], "example.org")
        result = self.build(base + "inner-auth = gtc\n")
        self.assertEqual(result["802-1x"]["phase2-autheap"], "gtc")

    def test_tls_with_certificate_files(self):
        for name in ("ca.pem", "user.p12"):
            (self.boot / name).write_text("data", encoding="utf-8")
        result = self.build(
            "ssid = Corp\nsecurity = wpa3-enterprise\neap = tls\nusername = u\nca-cert = ca.pem\n"
            "client-cert = user.p12\nprivate-key = user.p12\nprivate-key-password = kp\n"
        )
        self.assertEqual(result["wifi-security"]["key-mgmt"], "wpa-eap-suite-b-192")
        self.assertEqual(result["802-1x"]["ca-cert"], str(self.certs / "ca-cert.pem"))
        self.assertEqual(result["802-1x"]["private-key"], str(self.certs / "private-key.p12"))
        self.assertNotIn("password", result["802-1x"])

    def test_wpa3_enterprise_requires_tls(self):
        with self.assertRaises(ConfigError):
            self.build("ssid = C\nsecurity = wpa3-enterprise\nusername = u\npassword = p\nca-cert = none\n")

    def test_fast_and_pwd_and_dynamic_wep(self):
        result = self.build("ssid = C\nsecurity = dynamic-wep\neap = fast\nusername = u\npassword = p\n")
        self.assertEqual(result["wifi-security"]["key-mgmt"], "ieee8021x")
        self.assertEqual(result["802-1x"]["phase1-fast-provisioning"], "1")
        self.assertEqual(result["802-1x"]["pac-file"], str(self.certs / "fast.pac"))
        with self.assertRaises(ConfigError):
            self.build("ssid = C\nsecurity = wpa-enterprise\neap = fast\nfast-provisioning = disabled\n"
                       "username = u\npassword = p\n")
        result = self.build("ssid = C\nsecurity = wpa-enterprise\neap = pwd\nusername = u\npassword = p\n")
        self.assertEqual(result["802-1x"]["eap"], "pwd;")

    def test_rejects_unsafe_or_missing_files(self):
        for name in ("../etc/passwd", "/etc/passwd", "missing.pem"):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                self.build(f"ssid = C\nsecurity = wpa-enterprise\nusername = u\npassword = p\nca-cert = {name}\n")

    def test_rejects_settings_unused_by_security_type(self):
        with self.assertRaises(ConfigError):
            self.build("ssid = Home\npassword = longenough\npeap-version = 1\n")

    def test_ssid_with_semicolon_is_written_as_bytes(self):
        result = self.build("ssid = a;b\nsecurity = none\n")
        self.assertEqual(result["wifi"]["ssid"], "97;59;98;")


class ApplyTests(WifiTestCase):
    def setUp(self):
        super().setUp()
        self.connections = self.root / "connections"
        self.connections.mkdir()
        (self.boot / "ca.pem").write_text("certificate", encoding="utf-8")
        self.config.write_text(
            "ssid = Corp\r\nsecurity = wpa-enterprise\r\nusername = u\r\npassword = TopSecret\r\n"
            "ca-cert = ca.pem\r\n",
            encoding="utf-8",
        )

    @patch("kiosk_wifi.subprocess.run", return_value=MagicMock(returncode=0))
    def test_saves_private_connection_and_removes_secrets(self, run):
        apply(self.config, self.connections, self.certs)
        keyfile = self.connections / "kiosk-wifi.nmconnection"
        self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.certs / "ca-cert.pem").read_text(), "certificate")
        self.assertEqual((self.certs / "ca-cert.pem").stat().st_mode & 0o777, 0o600)
        run.assert_called_once_with(
            ["nmcli", "connection", "load", str(keyfile)], capture_output=True, text=True, check=False
        )
        self.assertFalse(self.config.exists())
        self.assertFalse((self.boot / "ca.pem").exists())
        applied = (self.boot / APPLIED_NAME).read_text(encoding="utf-8")
        self.assertNotIn("TopSecret", applied)
        self.assertIn("ssid = Corp", applied)

    @patch("kiosk_wifi.subprocess.run", return_value=MagicMock(returncode=4, stderr="Error: invalid"))
    def test_rejected_connection_is_removed_and_reported(self, run):
        with patch("kiosk_wifi.os.geteuid", return_value=0), \
                patch("kiosk_wifi.apply", side_effect=lambda config: apply(config, self.connections, self.certs)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config)]), 1)
        self.assertFalse((self.connections / "kiosk-wifi.nmconnection").exists())
        self.assertTrue(self.config.exists())
        self.assertIn("invalid", (self.boot / ERROR_NAME).read_text(encoding="utf-8"))

    @patch("kiosk_wifi.subprocess.run")
    def test_check_does_not_apply(self, run):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config), "--check"]), 0)
        run.assert_not_called()
        self.assertTrue(self.config.exists())


if __name__ == "__main__":
    unittest.main()
