import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.release import metadata, read_config, release_notes


class ReleaseTests(unittest.TestCase):
    def test_repository_metadata_targets_stable_kiosk(self):
        config = read_config()
        self.assertIn("Trixie", config["build"]["base_os"])
        self.assertIsInstance(config["prerelease"], bool)
        self.assertIn(f"tag=v{config['version']}", metadata(config))

    def test_version_override(self):
        config = read_config(version="1.2.3-rc.1")
        self.assertEqual(config["version"], "1.2.3-rc.1")
        self.assertIn(f"prerelease={str(config['prerelease']).lower()}", metadata(config))

    def test_rejects_unsafe_version_overrides(self):
        for version in ("1.0", "v1.0.0", "../image", "1.2.3\ntag=other", "$(id)", "1.2.3;id"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                read_config(version=version)

    def test_rejects_multiline_names_and_non_boolean_flags(self):
        for field, value in (("name", "Kiosk\ntag=other"), ("prerelease", "false"), ("draft", 1)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                config = read_config()
                config[field] = value
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_config(path)

    def test_notes_use_kiosk_names_and_disclose_hardware_validation(self):
        notes = release_notes(read_config(version="1.2.3-rc.1"), "test-commit")
        self.assertIn("RaspberryPi_Web_Kiosk_v1.2.3-rc.1.img.gz", notes)
        self.assertIn("kiosk-url.txt", notes)
        self.assertIn("physical hardware testing", notes)
        self.assertNotIn("HDMI Tester", notes)


class PackagingTests(unittest.TestCase):
    def test_checksum_generation_and_verification(self):
        script = Path(__file__).resolve().parents[1] / "scripts/generate-checksums.sh"
        with tempfile.TemporaryDirectory() as directory:
            deploy = Path(directory)
            archive = deploy / "RaspberryPi_Web_Kiosk_v1.2.3.img.gz"
            archive.write_bytes(gzip.compress(b"synthetic image for packaging test"))
            result = subprocess.run(
                ["bash", str(script), "1.2.3", str(deploy)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)
            checksum = deploy / "RaspberryPi_Web_Kiosk_v1.2.3.sha256"
            self.assertTrue(checksum.is_file())
            self.assertIn(archive.name, checksum.read_text())
            self.assertTrue((deploy / "VERIFY_DOWNLOAD_v1.2.3.txt").is_file())
            archive.write_bytes(b"corrupted download")
            verify = subprocess.run(
                ["sha256sum", "--check", checksum.name], cwd=deploy, capture_output=True
            )
            self.assertNotEqual(verify.returncode, 0)

    def test_rejects_invalid_archives(self):
        script = Path(__file__).resolve().parents[1] / "scripts/generate-checksums.sh"
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "RaspberryPi_Web_Kiosk_v1.2.3.img.gz"
            archive.write_bytes(b"not a gzip archive")
            result = subprocess.run(
                ["bash", str(script), "1.2.3", directory], capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(archive.with_suffix("").with_suffix(".sha256").exists())


if __name__ == "__main__":
    unittest.main()