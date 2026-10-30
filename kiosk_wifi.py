#!/usr/bin/env python3
"""Apply Wi-Fi settings from kiosk-wifi.txt on the readable boot partition."""

import argparse
import configparser
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


DEFAULT_CONFIG = Path("/boot/firmware/kiosk-wifi.txt")
CONNECTION_DIR = Path("/etc/NetworkManager/system-connections")
CERT_DIR = Path("/etc/raspberry-pi-web-kiosk/wifi")
QUARANTINE_DIR = Path("/var/lib/raspberry-pi-web-kiosk/wifi-quarantine")
CONNECTION_ID = "kiosk-wifi"
CONNECTION_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:raspberry-pi-web-kiosk:kiosk-wifi"))
APPLIED_NAME = "kiosk-wifi-applied.txt"
ERROR_NAME = "kiosk-wifi-error.txt"
MAX_FILE_SIZE = 1024 * 1024

# Matches the security choices in the Raspberry Pi OS desktop Wi-Fi dialog.
SECURITY = {
    "none": None,
    "owe": "owe",
    "wep": "none",
    "leap": "ieee8021x",
    "dynamic-wep": "ieee8021x",
    "wpa-personal": "wpa-psk",
    "wpa3-personal": "sae",
    "wpa-enterprise": "wpa-eap",
    "wpa3-enterprise": "wpa-eap-suite-b-192",
}
EAP_METHODS = ("peap", "ttls", "tls", "pwd", "fast", "leap")
INNER_AUTH = {
    "peap": {
        "mschapv2": ("phase2-auth", "mschapv2"),
        "md5": ("phase2-auth", "md5"),
        "gtc": ("phase2-auth", "gtc"),
    },
    "ttls": {
        "pap": ("phase2-auth", "pap"),
        "chap": ("phase2-auth", "chap"),
        "mschap": ("phase2-auth", "mschap"),
        "mschapv2": ("phase2-autheap", "mschapv2"),
        "mschapv2-no-eap": ("phase2-auth", "mschapv2"),
        "md5": ("phase2-autheap", "md5"),
        "gtc": ("phase2-autheap", "gtc"),
    },
    "fast": {
        "gtc": ("phase2-auth", "gtc"),
        "mschapv2": ("phase2-auth", "mschapv2"),
    },
}
FAST_PROVISIONING = {"anonymous": "1", "authenticated": "2", "both": "3", "disabled": "0"}
SECRET_KEYS = ("password", "private-key-password")
KNOWN_KEYS = frozenset(
    (
        "ssid", "hidden", "security", "password", "username",
        "wep-key-type", "wep-key-index", "wep-auth",
        "eap", "anonymous-identity", "domain", "ca-cert", "peap-version", "inner-auth",
        "client-cert", "private-key", "private-key-password", "fast-provisioning", "pac-file",
    )
)
SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")


class ConfigError(ValueError):
    """The Wi-Fi configuration cannot be used."""


class Connection(NamedTuple):
    keyfile: str
    files: list[tuple[Path, Path]]
    warnings: list[str]


class _Settings:
    def __init__(self, values: dict[str, str]):
        self.values = values
        self.used: set[str] = set()

    def get(self, key: str, required: bool = False, hint: str = "") -> str:
        self.used.add(key)
        value = self.values.get(key, "")
        if required and not value:
            raise ConfigError(f"'{key}' is required{hint}.")
        return value

    def choice(self, key: str, options, default: str) -> str:
        value = (self.get(key) or default).lower()
        if value not in options:
            raise ConfigError(f"'{key}' must be one of: {', '.join(options)}.")
        return value


def read_settings(path: Path = DEFAULT_CONFIG) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except UnicodeError as error:
        raise ConfigError(f"{path.name} must be saved as UTF-8 plain text.") from error
    except OSError as error:
        raise ConfigError(f"Cannot read Wi-Fi settings (errno {error.errno}).") from error

    settings: dict[str, str] = {}
    seen: set[str] = set()
    for number, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        # Never echo the line: it may be a misplaced password.
        if not separator or key not in KNOWN_KEYS:
            raise ConfigError(f"Line {number} is not a known 'setting = value' line.")
        if key in seen:
            raise ConfigError(f"'{key}' is set more than once.")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ConfigError(f"'{key}' contains invalid characters.")
        seen.add(key)
        if value:
            settings[key] = value
    return settings


def _add_file(settings: _Settings, key: str, boot_dir: Path, cert_dir: Path, files) -> str:
    name = settings.get(key, required=True)
    source = boot_dir / name
    if not SAFE_FILE_NAME.fullmatch(name) or source.is_symlink() or not source.is_file():
        raise ConfigError(f"'{key}' must name a file next to kiosk-wifi.txt on the boot partition.")
    if source.stat().st_size > MAX_FILE_SIZE:
        raise ConfigError(f"The '{key}' file is larger than 1 MiB.")
    destination = cert_dir / f"{key}{source.suffix.lower()}"
    files.append((source, destination))
    return str(destination)


def _eap_section(settings: _Settings, security: str, boot_dir: Path, cert_dir: Path,
                 files, warnings) -> dict[str, str]:
    eap = settings.choice("eap", EAP_METHODS, "peap")
    if security == "wpa3-enterprise" and eap != "tls":
        raise ConfigError("WPA3 Enterprise (192-bit) requires 'eap = tls'.")
    section = {"eap": f"{eap};", "identity": settings.get("username", required=True)}

    if eap in ("peap", "ttls", "fast") and settings.get("anonymous-identity"):
        section["anonymous-identity"] = settings.get("anonymous-identity")

    if eap in ("peap", "ttls", "tls"):
        if settings.get("domain"):
            section["domain-suffix-match"] = settings.get("domain")
        ca_cert = settings.get(
            "ca-cert", required=True,
            hint=": a certificate file name on the boot partition, 'system' or 'none'",
        )
        if ca_cert.lower() == "none":
            warnings.append("No CA certificate is required, so the network's identity is not verified.")
        elif ca_cert.lower() == "system":
            section["system-ca-certs"] = "true"
        else:
            section["ca-cert"] = _add_file(settings, "ca-cert", boot_dir, cert_dir, files)

    if eap == "peap":
        version = settings.choice("peap-version", ("auto", "0", "1"), "auto")
        if version != "auto":
            section["phase1-peapver"] = version

    if eap in INNER_AUTH:
        options = INNER_AUTH[eap]
        name, value = options[settings.choice("inner-auth", tuple(options), "mschapv2")]
        section[name] = value

    if eap == "fast":
        provisioning = settings.choice("fast-provisioning", tuple(FAST_PROVISIONING), "anonymous")
        section["phase1-fast-provisioning"] = FAST_PROVISIONING[provisioning]
        if settings.get("pac-file"):
            section["pac-file"] = _add_file(settings, "pac-file", boot_dir, cert_dir, files)
        elif provisioning == "disabled":
            raise ConfigError("'pac-file' is required when 'fast-provisioning = disabled'.")
        else:
            section["pac-file"] = str(cert_dir / "fast.pac")

    if eap == "tls":
        section["client-cert"] = _add_file(settings, "client-cert", boot_dir, cert_dir, files)
        section["private-key"] = _add_file(settings, "private-key", boot_dir, cert_dir, files)
        key_password = settings.get("private-key-password")
        if key_password:
            section["private-key-password"] = key_password
        else:
            section["private-key-password-flags"] = "4"
    else:
        section["password"] = settings.get("password", required=True)
    return section


def _ssid_value(ssid: str) -> str:
    # Keyfiles treat ';' as a list separator, so write such names as bytes.
    if ";" in ssid:
        return "".join(f"{byte};" for byte in ssid.encode())
    return ssid


def _render(sections: dict[str, dict[str, str]]) -> str:
    lines = []
    for name, values in sections.items():
        lines.append(f"[{name}]")
        lines.extend(f"{key}={value.replace(chr(92), chr(92) * 2)}" for key, value in values.items())
        lines.append("")
    return "\n".join(lines)


def build_connection(values: dict[str, str], boot_dir: Path, cert_dir: Path = CERT_DIR) -> Connection:
    settings = _Settings(values)
    files: list[tuple[Path, Path]] = []
    warnings: list[str] = []

    ssid = settings.get("ssid", required=True)
    if len(ssid.encode()) > 32:
        raise ConfigError("'ssid' must be at most 32 bytes.")
    wifi = {"mode": "infrastructure", "ssid": _ssid_value(ssid)}
    if settings.choice("hidden", ("no", "yes"), "no") == "yes":
        wifi["hidden"] = "true"
    sections = {
        "connection": {
            "id": CONNECTION_ID, "uuid": CONNECTION_UUID, "type": "wifi", "autoconnect": "true",
        },
        "wifi": wifi,
    }

    security = settings.choice("security", tuple(SECURITY), "wpa-personal")
    if SECURITY[security]:
        wifi_security = sections["wifi-security"] = {"key-mgmt": SECURITY[security]}

    if security == "wpa-personal":
        psk = settings.get("password", required=True)
        if not ((psk.isascii() and 8 <= len(psk) <= 63) or re.fullmatch(r"[0-9A-Fa-f]{64}", psk)):
            raise ConfigError("A WPA 'password' must be 8-63 characters or 64 hex digits.")
        wifi_security["psk"] = psk
    elif security == "wpa3-personal":
        wifi_security["psk"] = settings.get("password", required=True)
    elif security == "wep":
        key_type = settings.choice("wep-key-type", ("key", "passphrase"), "key")
        index = int(settings.choice("wep-key-index", ("1", "2", "3", "4"), "1")) - 1
        key = settings.get("password", required=True)
        if key_type == "key" and not (
            re.fullmatch(r"[0-9A-Fa-f]{10}|[0-9A-Fa-f]{26}", key)
            or (key.isascii() and len(key) in (5, 13))
        ):
            raise ConfigError("A WEP key must be 5 or 13 characters, or 10 or 26 hex digits.")
        wifi_security.update({
            "auth-alg": settings.choice("wep-auth", ("open", "shared"), "open"),
            "wep-key-type": "1" if key_type == "key" else "2",
            "wep-tx-keyidx": str(index),
            f"wep-key{index}": key,
        })
    elif security == "leap":
        wifi_security.update({
            "auth-alg": "leap",
            "leap-username": settings.get("username", required=True),
            "leap-password": settings.get("password", required=True),
        })
    elif security in ("dynamic-wep", "wpa-enterprise", "wpa3-enterprise"):
        sections["802-1x"] = _eap_section(settings, security, boot_dir, cert_dir, files, warnings)

    sections["ipv4"] = {"method": "auto"}
    sections["ipv6"] = {"addr-gen-mode": "default", "method": "auto"}

    unused = sorted(set(values) - settings.used)
    if unused:
        raise ConfigError(f"Not used with these security settings: {', '.join(unused)}.")
    return Connection(_render(sections), files, warnings)


def write_private(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".kiosk-wifi-")
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _archive(config: Path) -> None:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"# Applied {stamp}. Passwords and key files were removed from this drive.",
        "# To change Wi-Fi, copy this file to kiosk-wifi.txt and enter the passwords again.",
        "",
    ]
    for line in config.read_text(encoding="utf-8-sig").splitlines():
        key, separator, _ = line.partition("=")
        if separator and not line.lstrip().startswith("#") and key.strip().lower() in SECRET_KEYS:
            line = f"{key.strip()} ="
        lines.append(line)
    (config.parent / APPLIED_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\r\n")
    config.unlink()


def wifi_event(event: str, *, count: int | None = None, code: int | None = None,
               reference: str = "") -> None:
    """Log fixed event names and numeric diagnostics, never configuration or command output."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", event):
        raise ValueError("Invalid Wi-Fi event name.")
    fields = [f"Wi-Fi event={event}"]
    for key, value in (("count", count), ("code", code)):
        if value is not None:
            if type(value) is not int:
                raise ValueError("Wi-Fi diagnostics must be integers.")
            fields.append(f"{key}={value}")
    if reference:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", reference):
            raise ValueError("Invalid Wi-Fi recovery reference.")
        fields.append(f"reference={reference}")
    print(" ".join(fields), file=sys.stderr, flush=True)


def _nmcli(arguments: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["nmcli", *arguments], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        wifi_event("nmcli-timeout" if isinstance(error, subprocess.TimeoutExpired) else "nmcli-command-error",
                   code=error.errno if isinstance(error, OSError) else None)
        raise ConfigError(f"NetworkManager command failed ({type(error).__name__}).") from error


def _connection_profiles(active: bool = False) -> dict[str, str]:
    arguments = ["--terse", "--escape", "no", "--fields", "UUID,TYPE", "connection", "show"]
    if active:
        arguments.append("--active")
    result = _nmcli(arguments)
    if result.returncode != 0:
        wifi_event("profile-list-failed", code=result.returncode)
        raise ConfigError("Cannot list saved NetworkManager connections.")
    connections = {}
    for line in result.stdout.splitlines():
        identifier, separator, kind = line.partition(":")
        if separator:
            try:
                connections[str(uuid.UUID(identifier))] = kind
            except ValueError:
                wifi_event("profile-list-invalid-uuid")
                raise ConfigError("NetworkManager returned an invalid connection UUID.") from None
    return connections


def wifi_connection_uuids(active: bool = False) -> list[str]:
    return [
        identifier for identifier, kind in _connection_profiles(active).items()
        if kind in ("wifi", "802-11-wireless")
    ]


def quarantine_invalid_wifi_profiles(connection_dir: Path = CONNECTION_DIR,
                                     quarantine_dir: Path = QUARANTINE_DIR) -> list[Path]:
    wifi_event("quarantine-scan-start")
    loaded = set(_connection_profiles())
    quarantined = []
    for path in sorted(connection_dir.glob("*.nmconnection")):
        if path.is_symlink() or not path.is_file():
            continue
        profile = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            data = path.read_bytes()
            profile.read_string(data.decode("utf-8-sig"))
        except (OSError, UnicodeError, configparser.Error):
            wifi_event("quarantine-unclassified-file-preserved")
            continue
        kind = profile.get("connection", "type", fallback="")
        wireless = kind in ("wifi", "802-11-wireless") or (
            not kind and any(profile.has_section(section) for section in ("wifi", "802-11-wireless"))
        )
        identifier = profile.get("connection", "uuid", fallback="")
        with contextlib.suppress(ValueError):
            identifier = str(uuid.UUID(identifier))
        if not wireless or identifier in loaded:
            continue
        result = _nmcli(["connection", "load", str(path)])
        if result.returncode == 0:
            continue
        if _nmcli(["general", "status"]).returncode != 0:
            wifi_event("quarantine-daemon-unavailable")
            raise ConfigError("NetworkManager is unavailable; no further profiles will be quarantined.")
        if quarantine_dir.is_symlink():
            raise ConfigError("The Wi-Fi quarantine directory must not be a symbolic link.")
        quarantine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        quarantine_dir.chmod(0o700)
        reference = uuid.uuid4().hex
        destination = quarantine_dir / f"{reference}-{path.name}"
        write_private(destination, data)
        path.unlink()
        quarantined.append(destination)
        wifi_event("quarantine-profile-moved", code=result.returncode, reference=reference)
    wifi_event("quarantine-complete", count=len(quarantined))
    return quarantined


def reset_wifi_profiles(connection_dir: Path = CONNECTION_DIR,
                        quarantine_dir: Path = QUARANTINE_DIR) -> int:
    wifi_event("reset-start")
    removed = 0
    try:
        quarantine_invalid_wifi_profiles(connection_dir, quarantine_dir)
        identifiers = wifi_connection_uuids()
        wifi_event("reset-planned", count=len(identifiers))
        for identifier in identifiers:
            result = _nmcli(["connection", "delete", "uuid", identifier])
            if result.returncode != 0:
                wifi_event("reset-delete-failed", code=result.returncode)
                raise ConfigError("Wi-Fi reset stopped after a deletion failed; some profiles may already be removed.")
            removed += 1
            wifi_event("reset-profile-removed", count=removed)
    except (ConfigError, OSError):
        wifi_event("reset-failed", count=removed)
        raise
    wifi_event("reset-complete", count=removed)
    return removed


def save_connection(connection: Connection, connection_dir: Path = CONNECTION_DIR,
                    cert_dir: Path = CERT_DIR) -> None:
    wifi_event("save-start")
    previous_active = wifi_connection_uuids(active=True)
    wifi_event("save-previous-active", count=len(previous_active))
    cert_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    keyfile = connection_dir / f"{CONNECTION_ID}.nmconnection"
    destinations = [keyfile, *(destination for _, destination in connection.files)]
    backup_dir = Path(tempfile.mkdtemp(prefix=".kiosk-wifi-backup-", dir=connection_dir))
    backups = {}
    discard_backup = False
    try:
        for index, destination in enumerate(dict.fromkeys(destinations)):
            if destination.is_symlink():
                raise ConfigError("Cannot replace a symbolic link in the Wi-Fi settings.")
            backup = backup_dir / str(index)
            if destination.exists():
                write_private(backup, destination.read_bytes())
                backups[destination] = backup
            else:
                backups[destination] = None
        wifi_event("save-backup-ready", count=len(backups))
        try:
            for source, destination in connection.files:
                write_private(destination, source.read_bytes())
            write_private(keyfile, connection.keyfile.encode())
            wifi_event("save-load-start")
            result = _nmcli(["connection", "load", str(keyfile)])
            if result.returncode != 0:
                wifi_event("save-load-failed", code=result.returncode)
                raise ConfigError(f"NetworkManager rejected the Wi-Fi settings (nmcli exit code {result.returncode}).")
            wifi_event("save-activation-start")
            result = _nmcli(["--wait", "60", "connection", "up", "uuid", CONNECTION_UUID], timeout=70)
            if result.returncode != 0:
                wifi_event("save-activation-failed", code=result.returncode)
                detail = result.stderr.lower()
                if any(word in detail for word in ("secrets", "password", "authentication")):
                    wifi_event("save-authentication-required-or-rejected", code=result.returncode)
                    reason = "Authentication failed; check the Wi-Fi username or password."
                elif "could not be found" in detail or "no network with ssid" in detail:
                    wifi_event("save-network-not-found", code=result.returncode)
                    reason = "The Wi-Fi network could not be found."
                else:
                    reason = "Could not activate the new Wi-Fi connection."
                raise ConfigError(f"{reason} (nmcli exit code {result.returncode}).")
            discard_backup = True
            wifi_event("save-committed")
        except BaseException as error:
            wifi_event("save-aborted", code=error.errno if isinstance(error, OSError) else None)
            wifi_event("rollback-start")
            try:
                for destination, backup in backups.items():
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    else:
                        write_private(destination, backup.read_bytes())
            except OSError as error:
                wifi_event("rollback-files-failed", code=error.errno)
                raise
            wifi_event("rollback-files-restored", count=len(backups))
            try:
                if backups[keyfile] is not None:
                    result = _nmcli(["connection", "load", str(keyfile)])
                    restored = result.returncode == 0
                    wifi_event("rollback-reload-result", code=result.returncode)
                else:
                    result = _nmcli(["connection", "delete", "uuid", CONNECTION_UUID])
                    restored = result.returncode in (0, 10)
                    wifi_event("rollback-candidate-removal-result", code=result.returncode)
                wifi_event("rollback-reconnect-start", count=len(previous_active))
                for identifier in previous_active:
                    result = _nmcli(["--wait", "60", "connection", "up", "uuid", identifier], timeout=70)
                    wifi_event("rollback-reconnect-result", code=result.returncode)
                    restored = result.returncode == 0 and restored
                discard_backup = restored
            except ConfigError:
                pass
            wifi_event("rollback-complete" if discard_backup else "rollback-incomplete")
            raise
    finally:
        if discard_backup:
            shutil.rmtree(backup_dir)
        else:
            wifi_event("backup-retained", reference=backup_dir.name.removeprefix(".kiosk-wifi-backup-"))


def apply(config: Path = DEFAULT_CONFIG, connection_dir: Path = CONNECTION_DIR,
          cert_dir: Path = CERT_DIR) -> Connection:
    connection = build_connection(read_settings(config), config.parent, cert_dir)
    save_connection(connection, connection_dir, cert_dir)
    _archive(config)
    for source, _ in connection.files:
        source.unlink(missing_ok=True)
    (config.parent / ERROR_NAME).unlink(missing_ok=True)
    return connection


def _report_error(config: Path, message: str) -> None:
    with contextlib.suppress(OSError):
        (config.parent / ERROR_NAME).write_text(
            f"Wi-Fi settings were not applied.\n{message}\n", encoding="utf-8", newline="\r\n"
        )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true", help="Validate settings only.")
    options = parser.parse_args(arguments)

    try:
        values = read_settings(options.config)
        if not values:
            print(f"No Wi-Fi settings in {options.config}; nothing to apply.")
            return 0
        if options.check:
            connection = build_connection(values, options.config.parent)
            print(f"Valid Wi-Fi settings in {options.config}")
        else:
            if os.geteuid() != 0:
                raise ConfigError("Apply Wi-Fi settings as root.")
            connection = apply(options.config)
            wifi_event("provisioning-complete")
        for warning in connection.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    except (ConfigError, OSError) as error:
        wifi_event("provisioning-failed", code=error.errno if isinstance(error, OSError) else None)
        detail = (
            f"Wi-Fi file operation failed ({type(error).__name__}, errno {error.errno})."
            if isinstance(error, OSError) else str(error)
        )
        if not options.check:
            _report_error(options.config, detail)
        else:
            print(detail, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
