#!/usr/bin/env python3
"""Guided on-screen Wi-Fi setup for the Raspberry Pi Web Kiosk."""

import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from kiosk_wifi import (
    Connection, ConfigError, build_connection, quarantine_invalid_wifi_profiles,
    reset_wifi_profiles, save_connection, wifi_event,
)


BOOT_DIR = Path("/boot/firmware")
TITLE = "Web Kiosk network setup"
SECURITY_LABELS = {
    "wpa-enterprise": "username + password",
    "wpa-personal": "password",
    "wpa3-personal": "password (WPA3)",
    "wep": "WEP password, insecure",
    "owe": "open, encrypted",
    "none": "open, no password",
}
HIDDEN_SECURITY = (
    ("wpa-personal", "Password (WPA/WPA2)"),
    ("wpa3-personal", "Password (WPA3)"),
    ("wpa-enterprise", "Username + password (Enterprise)"),
    ("none", "Open, no password"),
)


class Cancelled(Exception):
    """The user pressed Cancel or Esc."""


class Network(NamedTuple):
    ssid: str
    security: str
    signal: int


def _run_ui(arguments: list[str], capture_answer: bool = False) -> subprocess.CompletedProcess:
    with open("/dev/tty", "w", encoding="utf-8") as terminal:
        return subprocess.run(
            arguments, stdin=sys.stdin, stdout=terminal,
            stderr=subprocess.PIPE if capture_answer else terminal,
            text=True, check=False,
        )


def _whiptail(*arguments: str) -> str:
    # whiptail draws on the terminal and writes the answer to stderr.
    result = _run_ui(["whiptail", "--title", TITLE, *arguments], capture_answer=True)
    if result.returncode != 0:
        raise Cancelled
    return result.stderr


def menu(text: str, items: list[tuple[str, str]], default: str = "") -> str:
    arguments = ["--notags", "--ok-button", "Select", "--cancel-button", "Back"]
    if default:
        arguments += ["--default-item", default]
    arguments += ["--menu", text, "20", "76", "10"]
    for tag, label in items:
        arguments += [tag, label]
    return _whiptail(*arguments)


def ask(text: str, secret: bool = False) -> str:
    while True:
        answer = _whiptail("--passwordbox" if secret else "--inputbox", text, "10", "76")
        if any(ord(character) < 32 or ord(character) == 127 for character in answer):
            message("That entry contains invalid characters. Please try again.")
        elif answer.strip():
            return answer if secret else answer.strip()
        else:
            message("This cannot be blank.")


def message(text: str) -> None:
    _whiptail("--msgbox", text, "14", "76")


def split_terse(line: str) -> list[str]:
    fields, current, escaped = [], [], False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    fields.append("".join(current))
    return fields


def security_from_scan(flags: str) -> str:
    words = set(flags.split())
    if "802.1X" in words:
        return "wpa-enterprise"
    if words & {"WPA1", "WPA2"}:
        return "wpa-personal"
    if "WPA3" in words:
        return "wpa3-personal"
    if "OWE" in words:
        return "owe"
    if "WEP" in words:
        return "wep"
    return "none"


def parse_scan(output: str) -> list[Network]:
    networks: dict[str, Network] = {}
    for line in output.splitlines():
        fields = split_terse(line)
        if len(fields) != 3 or not fields[0]:
            continue
        ssid, flags, signal = fields
        network = Network(ssid, security_from_scan(flags), int(signal) if signal.isdigit() else 0)
        if ssid not in networks or network.signal > networks[ssid].signal:
            networks[ssid] = network
    return sorted(networks.values(), key=lambda network: -network.signal)


def scan() -> list[Network]:
    _whiptail("--infobox", "Searching for Wi-Fi networks...", "7", "50")
    result = subprocess.run(
        ["nmcli", "--terse", "--escape", "yes", "--fields", "SSID,SECURITY,SIGNAL",
         "device", "wifi", "list", "--rescan", "yes"],
        capture_output=True, text=True, check=False,
    )
    return parse_scan(result.stdout)


def choose_network() -> tuple[str, str, bool]:
    while True:
        networks = scan()
        items = [
            (str(index), f"{network.ssid}  ({SECURITY_LABELS[network.security]}, {network.signal}%)")
            for index, network in enumerate(networks)
        ]
        items += [("hidden", "Hidden network (type its name)"), ("rescan", "Search again")]
        text = "Choose your Wi-Fi network:" if networks else "No networks found. Check the Wi-Fi is in range."
        choice = menu(text, items)
        if choice == "rescan":
            continue
        if choice == "hidden":
            ssid = ask("Network name (SSID), exactly as it is written:")
            security = menu(f"How does '{ssid}' sign in?", list(HIDDEN_SECURITY))
            return ssid, security, True
        network = networks[int(choice)]
        return network.ssid, network.security, False


def collect_settings(ssid: str, security: str, hidden: bool) -> dict[str, str]:
    values = {"ssid": ssid, "security": security}
    if hidden:
        values["hidden"] = "yes"
    if security in ("wpa-personal", "wpa3-personal", "wep"):
        values["password"] = ask(f"Wi-Fi password for '{ssid}':", secret=True)
    elif security == "wpa-enterprise":
        values["eap"] = "peap"
        values["username"] = ask(f"Username for '{ssid}':")
        values["password"] = ask(f"Password for '{ssid}':", secret=True)
        ca_cert = menu(
            "Does your IT team provide a CA certificate for this network?",
            [
                ("none", "No certificate (the network is not verified)"),
                ("system", "Use the built-in trusted certificates"),
                ("file", "Certificate file copied onto the boot partition"),
            ],
        )
        if ca_cert == "file":
            ca_cert = ask("Certificate file name on the boot partition, for example ca.pem:")
        values["ca-cert"] = ca_cert
        if ca_cert != "none":
            domain = _whiptail(
                "--inputbox", "Server domain from your IT team (optional, leave blank if unsure):",
                "10", "76",
            ).strip()
            if domain:
                values["domain"] = domain
    return values


def activate(ssid: str, connection: Connection) -> tuple[bool, str]:
    _whiptail("--infobox", f"Connecting to '{ssid}'...\nThis can take up to a minute.", "8", "60")
    try:
        save_connection(connection)
    except (ConfigError, OSError) as error:
        wifi_event("setup-activation-failed", code=error.errno if isinstance(error, OSError) else None)
        detail = str(error)
        if "secrets" in detail.lower():
            detail = "The network rejected the username or password. Please try again."
        elif "could not be found" in detail or "No network with SSID" in detail:
            detail = f"'{ssid}' could not be found. Check the Pi is within range."
        return False, f"{detail}\nPrevious Wi-Fi settings were preserved; see the log if reconnection failed."
    try:
        connectivity = subprocess.run(
            ["nmcli", "networking", "connectivity", "check"],
            capture_output=True, text=True, check=False, timeout=15,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        connectivity = "unknown"
    wifi_event({
        "full": "setup-connectivity-full", "portal": "setup-connectivity-portal",
        "limited": "setup-connectivity-limited", "none": "setup-connectivity-none",
    }.get(connectivity, "setup-connectivity-unknown"))
    if connectivity == "portal":
        return True, (
            f"Connected to '{ssid}', but the network requires a web login. "
            "The Pi signs in automatically if kiosk-portal.txt names the login page; "
            "otherwise the kiosk shows the login page."
        )
    return True, f"Connected to '{ssid}'. Settings are saved permanently."


def connect_wifi() -> bool:
    wifi_event("setup-connect-start")
    try:
        ssid, security, hidden = choose_network()
        connection = build_connection(collect_settings(ssid, security, hidden), BOOT_DIR)
    except Cancelled:
        wifi_event("setup-connect-cancelled")
        raise
    except (ConfigError, OSError) as error:
        wifi_event("setup-validation-failed", code=error.errno if isinstance(error, OSError) else None)
        message(f"These settings could not be saved:\n\n{error}")
        return False
    connected, result = activate(ssid, connection)
    wifi_event("setup-connect-complete" if connected else "setup-connect-failed")
    if connection.warnings:
        result += "\n\nNote: " + " ".join(connection.warnings)
    message(result)
    return connected


def status() -> str:
    result = subprocess.run(
        ["nmcli", "--terse", "--escape", "yes", "--fields", "TYPE,STATE,CONNECTION", "device"],
        capture_output=True, text=True, check=False,
    )
    lines = []
    for line in result.stdout.splitlines():
        fields = split_terse(line)
        if len(fields) == 3 and fields[0] in ("wifi", "ethernet") and fields[1].startswith("connected"):
            kind = "Wi-Fi" if fields[0] == "wifi" else "Ethernet"
            lines.append(f"{kind}: connected ({fields[2]})")
    return "\n".join(lines) or "Not connected."


def check_saved_wifi() -> None:
    try:
        quarantine_invalid_wifi_profiles()
    except (ConfigError, OSError) as error:
        wifi_event("profile-check-failed", code=error.errno if isinstance(error, OSError) else None)


def reset_saved_wifi() -> bool:
    wifi_event("reset-requested")
    try:
        _whiptail(
            "--defaultno", "--yes-button", "Reset Wi-Fi", "--no-button", "Cancel",
            "--yesno",
            "Remove all saved Wi-Fi connections? This disconnects Wi-Fi.\n\n"
            "Ethernet and VPN profiles will not be removed.\n"
            "Quarantined copies, certificates and boot files are retained.",
            "14", "76",
        )
    except Cancelled:
        wifi_event("reset-cancelled")
        return False
    wifi_event("reset-confirmed")
    try:
        count = reset_wifi_profiles()
    except (ConfigError, OSError) as error:
        wifi_event("reset-dialog-failed", code=error.errno if isinstance(error, OSError) else None)
        message(f"Wi-Fi reset could not finish:\n\n{error}")
        return False
    wifi_event("reset-dialog-complete", count=count)
    message(f"Removed {count} saved Wi-Fi connections.\nChoose Connect to Wi-Fi to configure a network.")
    return True


def main() -> int:
    wifi_event("setup-opened")
    check_saved_wifi()
    while True:
        try:
            choice = menu(
                f"{status()}\n\nWhat would you like to do?",
                [
                    ("wifi", "Connect to Wi-Fi"),
                    ("advanced", "Advanced network settings (all options)"),
                    ("reset", "Reset saved Wi-Fi connections"),
                    ("done", "Finish and start the kiosk"),
                ],
            )
        except Cancelled:
            wifi_event("setup-closed")
            return 0
        if choice == "done":
            wifi_event("setup-finished")
            return 0
        if choice == "advanced":
            wifi_event("advanced-opened")
            try:
                result = _run_ui(["nmtui"])
                wifi_event("advanced-closed", code=result.returncode)
            except OSError as error:
                wifi_event("advanced-failed", code=error.errno)
                message("Advanced network settings could not be opened.")
            check_saved_wifi()
            continue
        try:
            if choice == "reset":
                reset_saved_wifi()
            else:
                connect_wifi()
        except Cancelled:
            continue


if __name__ == "__main__":
    sys.exit(main())
