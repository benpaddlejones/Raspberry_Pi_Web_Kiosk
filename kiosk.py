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
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit


DEFAULT_CONFIG = Path("/boot/firmware/kiosk-url.txt")
PORTAL_WAIT_SECONDS = 180
MAX_SITE_REDIRECTS = 5


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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments, **keywords):
        return None


class Probe(NamedTuple):
    state: str  # open, portal or blocked
    location: str = ""


def probe_address(url: str) -> str:
    """Plain-HTTP address of the kiosk site; a login portal redirects it elsewhere."""
    address = urlsplit(url)
    host = f"[{address.hostname}]" if ":" in address.hostname else address.hostname
    port = f":{address.port}" if address.scheme == "http" and address.port else ""
    return f"http://{host}{port}{address.path or '/'}"


def _site_name(address: str) -> str:
    return (urlsplit(address).hostname or "").lower().removeprefix("www.")


def _request(target: str, timeout: float) -> Probe:
    site = _site_name(target)
    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(MAX_SITE_REDIRECTS + 1):
        try:
            with opener.open(target, timeout=timeout) as response:
                return Probe("open") if response.status == 200 else Probe("blocked")
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location", "") if 300 <= error.code < 400 else ""
            if not location:
                return Probe("blocked")
            try:
                destination = urljoin(target, location)
                same_site = _site_name(destination) == site
            except ValueError:
                return Probe("portal", location)
            if not same_site:
                return Probe("portal", destination)
            target = destination
        except OSError:
            return Probe("blocked")
    return Probe("blocked")


def probe_network(url: str, timeout: float = 10) -> Probe:
    """Signed in only if the kiosk page itself answers 200; anything else is not signed in."""
    if urlsplit(url).scheme == "http":
        return _request(url, timeout)
    # Unsigned networks normally bounce plain HTTP straight to their login page.
    plain = _request(probe_address(url), timeout)
    return plain if plain.state == "portal" else _request(url, timeout)


def portal_configured() -> bool:
    # Imported late because kiosk_portal imports this module.
    from kiosk_portal import DEFAULT_CONFIG as portal_config, PortalError, read_portal

    try:
        return read_portal(portal_config) is not None
    except PortalError:
        return False


def wait_for_login_page(url: str, limit: float = PORTAL_WAIT_SECONDS,
                        until_reachable: bool = False) -> None:
    """Wait for a network login to clear; with until_reachable, wait until the site answers 200."""
    deadline = time.monotonic() + limit
    while True:
        state = probe_network(url).state
        if state == "open" or (state == "blocked" and not until_reachable):
            return
        if time.monotonic() >= deadline:
            print("Network login still required; opening the browser.", file=sys.stderr, flush=True)
            return
        print("Waiting for the network login to complete.", file=sys.stderr, flush=True)
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
        wait_for_login_page(url, until_reachable=portal_configured())
        profile = Path.home() / ".local/share/raspberry-pi-web-kiosk/chromium"
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.execv(browser, browser_command(browser, url, profile))
    except (ConfigError, OSError) as error:
        print(f"Kiosk startup failed: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())