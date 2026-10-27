#!/usr/bin/env python3
"""Read release metadata and generate notes for the Build and Release workflow."""

import argparse
import json
import re
from pathlib import Path


CONFIG = Path(__file__).resolve().parents[1] / ".github/release-config.json"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")


def read_config(path: Path = CONFIG, version: str = "") -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    config["version"] = version or config["version"]
    if not isinstance(config["version"], str) or not VERSION_PATTERN.fullmatch(config["version"]):
        raise ValueError("Version must look like 1.2.3 or 1.2.3-alpha.1.")
    if not isinstance(config["name"], str) or not config["name"] or any(
        ord(character) < 32 or ord(character) == 127 for character in config["name"]
    ):
        raise ValueError("Release name must be a non-empty single line.")
    for field in ("prerelease", "draft"):
        if not isinstance(config[field], bool):
            raise ValueError(f"{field} must be a JSON boolean.")
    if not re.fullmatch(r"[0-9a-f]{40}", config["build"]["pi_gen_revision"]):
        raise ValueError("Pin pi-gen to a full Git commit hash.")
    return config


def metadata(config: dict) -> str:
    return "\n".join(
        (
            f"version={config['version']}",
            f"tag=v{config['version']}",
            f"name={config['name']}",
            f"prerelease={str(config['prerelease']).lower()}",
            f"draft={str(config['draft']).lower()}",
        )
    )


def release_notes(config: dict, commit: str) -> str:
    version = config["version"]
    lines = [
        f"# Raspberry Pi Web Kiosk v{version}",
        "",
        config["description"],
        "",
        f"Source commit: `{commit}`",
        f"Base OS: {config['build']['base_os']}",
        f"pi-gen revision: `{config['build']['pi_gen_revision']}`",
    ]
    for key, title in (
        ("features", "Features"),
        ("compatibility", "Target Hardware"),
        ("known_issues", "Known Limitations"),
    ):
        lines.extend(("", f"## {title}", ""))
        lines.extend(f"- {item}" for item in config["release_notes"][key])
    lines.extend(
        (
            "",
            "## Installation",
            "",
            f"1. Download `RaspberryPi_Web_Kiosk_v{version}.img.gz` and its `.sha256` file.",
            f"2. Verify with `sha256sum -c RaspberryPi_Web_Kiosk_v{version}.sha256`.",
            "3. Flash the compressed image using Raspberry Pi Imager (Use custom).",
            "4. Skip Imager's OS customisation: the image is preset for Sydney and needs no account setup.",
            "5. Reinsert the boot drive and edit `kiosk-url.txt` on `bootfs`.",
            "6. Eject safely, connect HDMI, a keyboard for on-screen Wi-Fi setup (or Ethernet), then boot.",
            "",
            f"[Setup and troubleshooting]({config['metadata']['documentation']})",
            "",
            "Image filesystem checks are automated; they do not replace physical hardware testing.",
        )
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("metadata", "notes"))
    parser.add_argument("--version", default="")
    parser.add_argument("--commit", default="unknown")
    options = parser.parse_args()
    try:
        config = read_config(version=options.version)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(metadata(config) if options.command == "metadata" else release_notes(config, options.commit))


if __name__ == "__main__":
    main()