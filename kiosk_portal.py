#!/usr/bin/env python3
"""Sign in to a network login page using the saved Wi-Fi username and password."""

import argparse
import contextlib
import hashlib
import http.client
import http.cookiejar
import json
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from kiosk import ConfigError, NoRedirect, probe_network, read_url
from kiosk_wifi import write_private


DEFAULT_CONFIG = Path("/boot/firmware/kiosk-portal.txt")
STATUS_NAME = "kiosk-portal-status.txt"
STATE_FILE = Path("/var/lib/raspberry-pi-web-kiosk/portal-state.json")
CHECK_SECONDS = 30
RETRY_SECONDS = 15 * 60
# After this many failures with the same credentials, try only once a day so
# the account is not locked out.
MAX_QUICK_FAILURES = 3
LONG_RETRY_SECONDS = 24 * 60 * 60
SIGN_IN_CHECKS = 5
SIGN_IN_CHECK_SECONDS = 2
REQUEST_TIMEOUT = 15
MAX_PAGE_BYTES = 1024 * 1024
HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST_PATTERN = re.compile(rf"{HOST_LABEL}(?:\.{HOST_LABEL})+")


class PortalError(Exception):
    """Sign-in cannot proceed. The message never contains credentials."""


class Credentials(NamedTuple):
    username: str
    password: str


def read_portal(path: Path = DEFAULT_CONFIG) -> str | None:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise PortalError(f"Cannot read {path.name}; save it as UTF-8 plain text.") from error

    hosts = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key.strip().lower() != "portal":
            raise PortalError("kiosk-portal.txt may only contain 'portal = host.name'.")
        if value.strip():
            hosts.append(value.strip().lower())
    if not hosts:
        return None
    if len(hosts) != 1 or not HOST_PATTERN.fullmatch(hosts[0]):
        raise PortalError("'portal' must be one host name such as portal.example.org.")
    return hosts[0]


def _nmcli(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["nmcli", *arguments], capture_output=True, text=True, check=False, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def read_credentials() -> Credentials:
    active = _nmcli("--terse", "--fields", "UUID,TYPE", "connection", "show", "--active")
    uuids = [
        line.partition(":")[0] for line in active.splitlines()
        if line.partition(":")[2] == "802-11-wireless"
    ]
    if not uuids:
        raise PortalError("Not connected to Wi-Fi, so there are no saved credentials to use.")
    for uuid in uuids:
        def value(setting: str, *options: str) -> str:
            return _nmcli(
                "--escape", "no", *options, "--get-values", setting, "connection", "show", "uuid", uuid
            ).rstrip("\n")

        username = value("802-1x.identity")
        password = value("802-1x.password", "--show-secrets")
        if username and password:
            return Credentials(username, password)
    raise PortalError(
        "The Wi-Fi connection has no saved username and password. "
        "Re-enter them in the network setup screen."
    )


def parse_login_page(body: str) -> str:
    if 'id="passwd"' not in body:
        raise PortalError("The login page was not recognised, so nothing was sent.")
    status = re.search(r'var respStatus = "([^"]*)"', body)
    if status and status.group(1) == "Challenge":
        raise PortalError("The login page asks for extra verification, which is not supported.")
    preauth = re.search(r'preauthid\.value = "([0-9A-Za-z]*)"', body)
    return preauth.group(1) if preauth else ""


def login_form(credentials: Credentials, preauth_id: str) -> dict[str, str]:
    return {
        "inputStr": "",
        "escapeUser": credentials.username.replace("\\", "\\\\"),
        "preauthid": preauth_id,
        "user": credentials.username,
        "passwd": credentials.password,
        "ok": "Login",
    }


def make_opener():
    return urllib.request.build_opener(
        NoRedirect,
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def _unreachable(error: Exception) -> PortalError:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLError):
        return PortalError("The login page's certificate is not trusted, so nothing was sent.")
    return PortalError("Could not reach the login page.")


def fetch(opener, url: str, form: dict[str, str] | None = None) -> tuple[int, str]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    try:
        with opener.open(urllib.request.Request(url, data=data), timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read(MAX_PAGE_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_PAGE_BYTES).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as error:
        raise _unreachable(error) from error


def load_state(path: Path) -> dict:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    failures, last = stored.get("failures"), stored.get("last_failure")
    return {
        "salt": stored["salt"] if isinstance(stored.get("salt"), str) and stored["salt"] else secrets.token_hex(16),
        "fingerprint": stored["fingerprint"] if isinstance(stored.get("fingerprint"), str) else "",
        "failures": failures if isinstance(failures, int) and failures > 0 else 0,
        "last_failure": float(last) if isinstance(last, (int, float)) else 0.0,
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, json.dumps(state).encode())


def fingerprint(state: dict, credentials: Credentials) -> str:
    material = f"{state['salt']}\0{credentials.username}\0{credentials.password}"
    return hashlib.sha256(material.encode()).hexdigest()


def retry_message(state: dict, now: float) -> str | None:
    """Return why a new sign-in must wait, or None when one is allowed."""
    if not state["failures"]:
        return None
    quick = state["failures"] < MAX_QUICK_FAILURES
    allowed_at = min(state["last_failure"], now) + (RETRY_SECONDS if quick else LONG_RETRY_SECONDS)
    if now >= allowed_at:
        return None
    when = datetime.fromtimestamp(allowed_at).strftime("%Y-%m-%d %H:%M")
    if quick:
        return f"The last sign-in was not accepted. Next attempt after {when}."
    return (
        f"Sign-in has failed {state['failures']} times. Trying once a day until the Wi-Fi "
        f"password changes. Next attempt after {when}."
    )


def check_once(config: Path = DEFAULT_CONFIG, state_path: Path = STATE_FILE,
               now: float | None = None) -> str:
    now = time.time() if now is None else now
    host = read_portal(config)
    if host is None:
        return "Portal sign-in is off: no 'portal' is set in kiosk-portal.txt."
    try:
        site = read_url(config.parent / "kiosk-url.txt")
    except ConfigError as error:
        raise PortalError("Cannot read the website from kiosk-url.txt.") from error

    probe = probe_network(site)
    if probe.state == "blocked":
        detail = f" ({probe.detail})" if probe.detail else ""
        return (
            "The kiosk website is not answering and the network did not offer a login page, "
            f"so nothing was sent.{detail}"
        )
    if probe.state == "open":
        state = load_state(state_path)
        if state["failures"]:
            save_state(state_path, {**state, "failures": 0})
        return "The kiosk website answered; no sign-in needed."

    try:
        address = urllib.parse.urlsplit(probe.location)
    except ValueError as error:
        raise PortalError("The login page address is not valid, so nothing was sent.") from error
    if address.hostname != host:
        shown = re.sub(r"[^A-Za-z0-9.-]", "?", address.hostname or "")[:100]
        raise PortalError(f"The login page is on '{shown}', not '{host}', so no credentials were sent.")
    if address.scheme != "https":
        raise PortalError("The login page is not HTTPS, so no credentials were sent.")

    credentials = read_credentials()
    state = load_state(state_path)
    current = fingerprint(state, credentials)
    if state["fingerprint"] != current:
        state.update(fingerprint=current, failures=0)
    waiting = retry_message(state, now)
    if waiting:
        return waiting

    opener = make_opener()
    status, body = fetch(opener, probe.location)
    if status != 200:
        raise PortalError(f"The login page returned status {status}, so nothing was sent.")
    preauth_id = parse_login_page(body)

    # Count the attempt before sending so a crash cannot cause unlimited retries.
    state.update(failures=state["failures"] + 1, last_failure=now)
    save_state(state_path, state)
    fetch(opener, probe.location, login_form(credentials, preauth_id))
    for _ in range(SIGN_IN_CHECKS):
        time.sleep(SIGN_IN_CHECK_SECONDS)
        if probe_network(site).state == "open":
            save_state(state_path, {**state, "failures": 0})
            return "Signed in to the network login page."
    return retry_message(state, now) or "The sign-in was not accepted."


def write_status(config: Path, message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    with contextlib.suppress(OSError):
        (config.parent / STATUS_NAME).write_text(
            f"{stamp}\n{message}\n", encoding="utf-8", newline="\r\n"
        )


def run(config: Path) -> None:
    previous = None
    while True:
        try:
            message = check_once(config)
        except PortalError as error:
            message = str(error)
        except Exception as error:  # A daemon that crashes would be restarted in a loop.
            message = f"Unexpected error ({type(error).__name__}); will try again."
        if message != previous:
            print(message, flush=True)
            write_status(config, message)
            previous = message
        time.sleep(CHECK_SECONDS)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true", help="Validate configuration only.")
    parser.add_argument(
        "--enabled", action="store_true",
        help="Exit 0 only when a portal is configured (the service's start condition).",
    )
    options = parser.parse_args(arguments)

    if options.enabled:
        try:
            host = read_portal(options.config)
        except PortalError as error:
            print(f"Portal sign-in not started: {error}", flush=True)
            write_status(options.config, str(error))
            return 1
        if host is None:
            print("Portal sign-in is off; not started.", flush=True)
            with contextlib.suppress(OSError):
                (options.config.parent / STATUS_NAME).unlink(missing_ok=True)
            return 1
        return 0

    if options.check:
        try:
            host = read_portal(options.config)
        except PortalError as error:
            print(f"Portal setup failed: {error}", file=sys.stderr)
            return 1
        print(f"Portal sign-in is on for {host}." if host else "Portal sign-in is off.")
        return 0
    run(options.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
