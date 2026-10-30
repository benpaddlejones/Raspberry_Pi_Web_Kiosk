import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from kiosk_wifi import (
    APPLIED_NAME, CONNECTION_UUID, ERROR_NAME, ConfigError, apply, build_connection, main, read_settings,
    quarantine_invalid_wifi_profiles, reset_wifi_profiles,
    wifi_event, write_private,
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
        self.log = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
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


class ProfileMaintenanceTests(WifiTestCase):
    def setUp(self):
        super().setUp()
        self.connections = self.root / "connections"
        self.connections.mkdir()
        self.quarantine = self.root / "quarantine"

    def profile(self, name, content):
        path = self.connections / f"{name}.nmconnection"
        path.write_text(content, encoding="utf-8")
        return path

    @patch("kiosk_wifi.subprocess.run")
    def test_quarantines_rejected_wifi_but_preserves_other_and_unknown_profiles(self, run):
        broken = self.profile("broken", "[connection]\nid=School\n[wifi]\nssid=School\n[ wifi-secret ]\npassword=PrivateSecret\n")
        original = broken.read_bytes()
        protected = [
            self.profile("wired", "[connection]\ntype=ethernet\n"),
            self.profile("vpn", "[connection]\ntype=vpn\n[wifi]\nssid=DoNotTouch\n"),
            self.profile("unknown", "[connection]\nid=Unclassified\n"),
            self.profile("unparseable", "not an INI file\n"),
        ]
        run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1), MagicMock(returncode=0),
        ]
        with contextlib.redirect_stderr(io.StringIO()) as log:
            moved = quarantine_invalid_wifi_profiles(self.connections, self.quarantine)
        self.assertFalse(broken.exists())
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].read_bytes(), original)
        self.assertEqual(moved[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.quarantine.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.exists() for path in protected))
        self.assertNotIn("PrivateSecret", log.getvalue())
        for value in ("School", "broken.nmconnection", "unparseable.nmconnection"):
            self.assertNotIn(value, log.getvalue())
        self.assertIn("event=quarantine-profile-moved code=1 reference=", log.getvalue())

    @patch("kiosk_wifi.subprocess.run")
    def test_valid_or_loaded_wifi_profiles_are_not_quarantined(self, run):
        loaded = self.profile("loaded", f"[connection]\ntype=wifi\nuuid={CONNECTION_UUID.upper()}\n")
        valid = self.profile("valid", "[connection]\ntype=wifi\n[wifi]\nssid=Home\n")
        run.side_effect = [
            MagicMock(returncode=0, stdout=f"{CONNECTION_UUID}:802-11-wireless\n"),
            MagicMock(returncode=0),
        ]
        self.assertEqual(quarantine_invalid_wifi_profiles(self.connections, self.quarantine), [])
        self.assertTrue(loaded.exists())
        self.assertTrue(valid.exists())
        self.assertFalse(self.quarantine.exists())

    @patch("kiosk_wifi.subprocess.run")
    def test_quarantine_does_not_reload_uuid_already_used_by_ethernet(self, run):
        path = self.profile("duplicate", f"[connection]\ntype=wifi\nuuid={CONNECTION_UUID}\n")
        run.return_value = MagicMock(returncode=0, stdout=f"{CONNECTION_UUID}:802-3-ethernet\n")
        self.assertEqual(quarantine_invalid_wifi_profiles(self.connections, self.quarantine), [])
        self.assertTrue(path.exists())
        run.assert_called_once()

    @patch("kiosk_wifi.subprocess.run")
    def test_daemon_failure_does_not_quarantine_profiles(self, run):
        path = self.profile("wifi", "[connection]\ntype=wifi\n")
        run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1), MagicMock(returncode=1),
        ]
        with self.assertRaises(ConfigError):
            quarantine_invalid_wifi_profiles(self.connections, self.quarantine)
        self.assertTrue(path.exists())
        self.assertFalse(self.quarantine.exists())

    @patch("kiosk_wifi.subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
    def test_symbolic_links_are_not_examined_or_moved(self, run):
        external = self.root / "external"
        external.write_text("[connection]\ntype=wifi\n", encoding="utf-8")
        link = self.connections / "linked.nmconnection"
        link.symlink_to(external)
        self.assertEqual(quarantine_invalid_wifi_profiles(self.connections, self.quarantine), [])
        self.assertTrue(link.is_symlink())
        self.assertTrue(external.exists())
        run.assert_called_once()

    @patch("kiosk_wifi.quarantine_invalid_wifi_profiles")
    @patch("kiosk_wifi.subprocess.run")
    def test_reset_deletes_only_wifi_by_uuid(self, run, quarantine):
        wireless = "a0b7ad05-96e4-4bd3-8ef9-9b96a2594d6e"
        wired = "51d85b46-bf51-42d7-b22b-210d8f770505"
        vpn = "10e056c6-45fa-4273-aa13-d60eb8122f17"
        run.side_effect = [
            MagicMock(returncode=0, stdout=f"{wireless}:802-11-wireless\n{wired}:802-3-ethernet\n{vpn}:vpn\n"),
            MagicMock(returncode=0),
        ]
        self.assertEqual(reset_wifi_profiles(self.connections, self.quarantine), 1)
        self.assertEqual(run.call_args.args[0], ["nmcli", "connection", "delete", "uuid", wireless])
        quarantine.assert_called_once_with(self.connections, self.quarantine)
        self.assertIn("event=reset-complete count=1", self.log.getvalue())
        for identifier in (wireless, wired, vpn):
            self.assertNotIn(identifier, self.log.getvalue())

    @patch("kiosk_wifi.quarantine_invalid_wifi_profiles")
    @patch("kiosk_wifi.subprocess.run", return_value=MagicMock(returncode=1))
    def test_failed_enumeration_does_not_delete_anything(self, run, quarantine):
        with self.assertRaises(ConfigError):
            reset_wifi_profiles(self.connections, self.quarantine)
        run.assert_called_once()

    @patch("kiosk_wifi.quarantine_invalid_wifi_profiles")
    @patch("kiosk_wifi.subprocess.run")
    def test_reset_stops_when_a_deletion_fails(self, run, quarantine):
        other = "a0b7ad05-96e4-4bd3-8ef9-9b96a2594d6e"
        run.side_effect = [
            MagicMock(returncode=0, stdout=f"{CONNECTION_UUID}:wifi\n{other}:wifi\n"),
            MagicMock(returncode=1),
        ]
        with self.assertRaises(ConfigError):
            reset_wifi_profiles(self.connections, self.quarantine)
        self.assertEqual(run.call_count, 2)
        self.assertIn("event=reset-delete-failed code=1", self.log.getvalue())
        self.assertIn("event=reset-failed count=0", self.log.getvalue())

    @patch("kiosk_wifi.quarantine_invalid_wifi_profiles")
    @patch("kiosk_wifi.subprocess.run", return_value=MagicMock(returncode=0, stdout="not-a-uuid:wifi\n"))
    def test_invalid_uuid_prevents_reset(self, run, quarantine):
        with self.assertRaises(ConfigError):
            reset_wifi_profiles(self.connections, self.quarantine)
        run.assert_called_once()


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

    @patch("kiosk_wifi.subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
    def test_saves_private_connection_and_removes_secrets(self, run):
        apply(self.config, self.connections, self.certs)
        keyfile = self.connections / "kiosk-wifi.nmconnection"
        self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.certs / "ca-cert.pem").read_text(), "certificate")
        self.assertEqual((self.certs / "ca-cert.pem").stat().st_mode & 0o777, 0o600)
        self.assertIn(call(
            ["nmcli", "connection", "load", str(keyfile)], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=15,
        ), run.call_args_list)
        self.assertEqual(run.call_args.args[0], [
            "nmcli", "--wait", "60", "connection", "up", "uuid", CONNECTION_UUID,
        ])
        self.assertFalse(self.config.exists())
        self.assertFalse((self.boot / "ca.pem").exists())
        applied = (self.boot / APPLIED_NAME).read_text(encoding="utf-8")
        self.assertNotIn("TopSecret", applied)
        self.assertIn("ssid = Corp", applied)
        self.assertIn("event=save-activation-start", self.log.getvalue())
        self.assertIn("event=save-committed", self.log.getvalue())
        for secret in ("Corp", "TopSecret", CONNECTION_UUID, str(self.certs)):
            self.assertNotIn(secret, self.log.getvalue())

    @patch("kiosk_wifi.subprocess.run")
    def test_rejected_connection_is_removed_and_reported(self, run):
        run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=4, stderr="Error: invalid PRIVATE RAW ERROR password = SECRET VALUE"),
            MagicMock(returncode=10),
        ]
        with patch("kiosk_wifi.os.geteuid", return_value=0), \
                patch("kiosk_wifi.apply", side_effect=lambda config: apply(config, self.connections, self.certs)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config)]), 1)
        self.assertFalse((self.connections / "kiosk-wifi.nmconnection").exists())
        self.assertTrue(self.config.exists())
        report = (self.boot / ERROR_NAME).read_text(encoding="utf-8")
        self.assertIn("exit code 4", report)
        self.assertNotIn("PRIVATE", report)
        self.assertNotIn("SECRET", report)

    @patch("kiosk_wifi.subprocess.run")
    def test_check_does_not_apply(self, run):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config), "--check"]), 0)
        run.assert_not_called()
        self.assertTrue(self.config.exists())

    @patch("kiosk_wifi.subprocess.run")
    def test_rejected_replacement_restores_profile_and_certificate(self, run):
        keyfile = self.connections / "kiosk-wifi.nmconnection"
        keyfile.write_bytes(b"previous profile")
        self.certs.mkdir()
        certificate = self.certs / "ca-cert.pem"
        certificate.write_bytes(b"previous certificate")
        run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=4, stderr="invalid replacement"),
            MagicMock(returncode=0),
        ]
        with self.assertRaises(ConfigError):
            apply(self.config, self.connections, self.certs)
        self.assertEqual(keyfile.read_bytes(), b"previous profile")
        self.assertEqual(certificate.read_bytes(), b"previous certificate")
        self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.config.exists())
        self.assertTrue((self.boot / "ca.pem").exists())
        self.assertFalse((self.boot / APPLIED_NAME).exists())
        self.assertEqual(list(self.connections.glob(".kiosk-wifi-backup-*")), [])

    @patch("kiosk_wifi.subprocess.run")
    def test_failed_activation_restores_and_reconnects_previous_wifi(self, run):
        keyfile = self.connections / "kiosk-wifi.nmconnection"
        keyfile.write_bytes(b"previous profile")
        run.side_effect = [
            MagicMock(returncode=0, stdout=f"{CONNECTION_UUID}:802-11-wireless\n"),
            MagicMock(returncode=0),
            MagicMock(returncode=4, stderr="Secrets were required"),
            MagicMock(returncode=0),
            MagicMock(returncode=0),
        ]
        with self.assertRaises(ConfigError):
            apply(self.config, self.connections, self.certs)
        self.assertEqual(keyfile.read_bytes(), b"previous profile")
        self.assertFalse((self.certs / "ca-cert.pem").exists())
        self.assertTrue(self.config.exists())
        self.assertEqual(run.call_args.args[0], [
            "nmcli", "--wait", "60", "connection", "up", "uuid", CONNECTION_UUID,
        ])
        for event in ("save-activation-failed code=4", "rollback-start", "rollback-files-restored",
                      "rollback-reconnect-result code=0", "rollback-complete"):
            self.assertIn(f"event={event}", self.log.getvalue())

    def test_event_fields_reject_unstructured_output(self):
        for fields in ({"count": "PRIVATE SECRET"}, {"code": "password=SECRET"},
                       {"reference": "/private/profile-name"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                wifi_event("test", **fields)
        self.assertEqual(self.log.getvalue(), "")

    @patch("kiosk_wifi.subprocess.run")
    def test_activation_error_does_not_expose_raw_command_output(self, run):
        run.side_effect = [
            MagicMock(returncode=0, stdout=""), MagicMock(returncode=0),
            MagicMock(returncode=4, stderr="Secrets were required: PRIVATE USER password = PRIVATE PASSWORD"),
            MagicMock(returncode=0),
        ]
        with self.assertRaises(ConfigError) as caught:
            apply(self.config, self.connections, self.certs)
        self.assertIn("username or password", str(caught.exception))
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn("PRIVATE", self.log.getvalue())

    @patch("kiosk_wifi.subprocess.run")
    def test_failed_rollback_write_logs_only_errno_and_retains_backup(self, run):
        keyfile = self.connections / "kiosk-wifi.nmconnection"
        keyfile.write_bytes(b"previous profile")
        run.side_effect = [
            MagicMock(returncode=0, stdout=""), MagicMock(returncode=0),
            MagicMock(returncode=4, stderr="PRIVATE activation failure"),
        ]

        def write(path, data):
            if path == keyfile and data == b"previous profile":
                raise OSError(5, "PRIVATE exception text", "/PRIVATE/FILENAME")
            write_private(path, data)

        with patch("kiosk_wifi.write_private", side_effect=write), self.assertRaises(OSError):
            apply(self.config, self.connections, self.certs)
        self.assertIn("event=rollback-files-failed code=5", self.log.getvalue())
        self.assertIn("event=backup-retained reference=", self.log.getvalue())
        self.assertNotIn("PRIVATE", self.log.getvalue())
        self.assertNotIn(str(self.connections), self.log.getvalue())
        backup = next(self.connections.glob(".kiosk-wifi-backup-*"))
        self.assertEqual((backup / "0").read_bytes(), b"previous profile")

    @patch("kiosk_wifi.subprocess.run")
    def test_failed_reconnection_retains_private_recovery_backup(self, run):
        keyfile = self.connections / "kiosk-wifi.nmconnection"
        keyfile.write_bytes(b"previous profile")
        run.side_effect = [
            MagicMock(returncode=0, stdout=f"{CONNECTION_UUID}:802-11-wireless\n"),
            MagicMock(returncode=0), MagicMock(returncode=4, stderr="unavailable"),
            MagicMock(returncode=0), MagicMock(returncode=4),
        ]
        with self.assertRaises(ConfigError), contextlib.redirect_stderr(io.StringIO()):
            apply(self.config, self.connections, self.certs)
        backup = next(self.connections.glob(".kiosk-wifi-backup-*"))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / "0").read_bytes(), b"previous profile")

    @patch("kiosk_wifi.subprocess.run")
    def test_failed_first_managed_connection_reactivates_other_previous_wifi(self, run):
        previous = "a0b7ad05-96e4-4bd3-8ef9-9b96a2594d6e"
        run.side_effect = [
            MagicMock(returncode=0, stdout=f"{previous}:802-11-wireless\n"),
            MagicMock(returncode=0), MagicMock(returncode=4, stderr="wrong password"),
            MagicMock(returncode=0), MagicMock(returncode=0),
        ]
        with self.assertRaises(ConfigError):
            apply(self.config, self.connections, self.certs)
        self.assertFalse((self.connections / "kiosk-wifi.nmconnection").exists())
        self.assertFalse((self.certs / "ca-cert.pem").exists())
        self.assertEqual(run.call_args_list[-2].args[0], [
            "nmcli", "connection", "delete", "uuid", CONNECTION_UUID,
        ])
        self.assertEqual(run.call_args.args[0], [
            "nmcli", "--wait", "60", "connection", "up", "uuid", previous,
        ])

    @patch("kiosk_wifi.subprocess.run")
    def test_activation_timeout_rolls_back(self, run):
        keyfile = self.connections / "kiosk-wifi.nmconnection"
        keyfile.write_bytes(b"previous profile")
        run.side_effect = [
            MagicMock(returncode=0, stdout=""), MagicMock(returncode=0),
            subprocess.TimeoutExpired("nmcli", 70), MagicMock(returncode=0),
        ]
        with self.assertRaises(ConfigError):
            apply(self.config, self.connections, self.certs)
        self.assertEqual(keyfile.read_bytes(), b"previous profile")
        self.assertFalse((self.certs / "ca-cert.pem").exists())
        self.assertTrue(self.config.exists())

    @patch("kiosk_wifi.subprocess.run", return_value=MagicMock(returncode=1))
    def test_no_settings_are_written_when_networkmanager_is_unavailable(self, run):
        keyfile = self.connections / "kiosk-wifi.nmconnection"
        keyfile.write_bytes(b"previous profile")
        with self.assertRaises(ConfigError):
            apply(self.config, self.connections, self.certs)
        self.assertEqual(keyfile.read_bytes(), b"previous profile")
        self.assertFalse(self.certs.exists())
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
