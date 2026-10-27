#!/usr/bin/env python3
"""Launch a Raspberry Pi web kiosk configured on the readable boot partition."""

import argparse
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_CONFIG = Path("/boot/firmware/kiosk-url.txt")
PORTAL_PROBE = "http://connectivitycheck.gstatic.com/generate_204"
PORTAL_WAIT_SECONDS = 180


class ConfigError(ValueError):
    """The kiosk configuration cannot be used."""


def read_url(path: Path = DEFAULT_CONFIG) -> str:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ConfigError(f"{path} must contain exactly one website URL.")

    url = lines[0]
    if "\\" in url or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in url
    ):
        raise ConfigError("The website URL contains whitespace or invalid characters.")

    try:
        parsed = urlsplit(url)
        valid_port = parsed.port is None or parsed.port > 0
        valid_address = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as error:
        raise ConfigError("The website URL is malformed.") from error

    if not valid_address or not valid_port or has_credentials:
        raise ConfigError("Use an http:// or https:// URL without embedded credentials.")

    return url


def browser_command(browser: str, url: str, profile: Path) -> list[str]:
    return [
        browser,
        "--kiosk",
        "--ozone-platform=wayland",
        "--no-first-run",
        "--no-default-browser-check",
        "--noerrdialogs",
        "--disable-session-crashed-bubble",
        f"--user-data-dir={profile}",
        url,
    ]


def wait_for_network(url: str) -> None:
    address = urlsplit(url)
    port = address.port or (443 if address.scheme == "https" else 80)
    while True:
        try:
            with socket.create_connection((address.hostname, port), timeout=5):
                return
        except OSError:
            print("Website host unavailable; retrying in 5 seconds.", file=sys.stderr, flush=True)
            time.sleep(5)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments, **keywords):
        return None


def behind_login_page() -> bool:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(PORTAL_PROBE, timeout=10) as response:
            return response.status != 204
    except urllib.error.HTTPError as error:
        return 300 <= error.code < 400
    except OSError:
        # Probe unreachable (e.g. intranet-only network): don't delay the kiosk.
        return False


def wait_for_login_page(limit: float = PORTAL_WAIT_SECONDS) -> None:
    deadline = time.monotonic() + limit
    while behind_login_page():
        if time.monotonic() >= deadline:
            print("Network login page still required; opening the browser.", file=sys.stderr, flush=True)
            return
        print("Network login page detected; waiting for sign-in to clear.", file=sys.stderr, flush=True)
        time.sleep(5)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true", help="Validate configuration only.")
    options = parser.parse_args(arguments)

    try:
        url = read_url(options.config)
        if options.check:
            print(f"Valid kiosk URL in {options.config}")
            return 0

        if os.geteuid() == 0:
            raise ConfigError("Run the browser as the unprivileged kiosk user, not root.")

        browser = shutil.which("chromium") or shutil.which("chromium-browser")
        if browser is None:
            raise ConfigError("Chromium is not installed.")

        wait_for_network(url)
        wait_for_login_page()
        profile = Path.home() / ".local/share/raspberry-pi-web-kiosk/chromium"
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.execv(browser, browser_command(browser, url, profile))
    except (ConfigError, OSError) as error:
        print(f"Kiosk startup failed: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())