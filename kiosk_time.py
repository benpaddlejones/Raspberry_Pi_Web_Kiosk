#!/usr/bin/env python3
"""Configure systemd-timesyncd from the readable boot partition before it starts."""

import argparse
import ipaddress
import re
import sys
from pathlib import Path


DEFAULT_CONFIG = Path("/boot/firmware/kiosk-time.txt")
DEFAULT_OUTPUT = Path("/run/systemd/timesyncd.conf.d/zzz-web-kiosk.conf")
DEFAULT_SERVERS = ("time.windows.com",)


class TimeConfigError(ValueError):
    """The time server configuration cannot be used."""


def read_servers(path: Path = DEFAULT_CONFIG) -> tuple[str, ...]:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return DEFAULT_SERVERS
    except (OSError, UnicodeError) as error:
        raise TimeConfigError(f"Cannot read {path}: {error}") from error

    servers = []
    for number, line in enumerate(content.splitlines(), start=1):
        server = line.partition("#")[0].strip()
        if not server:
            continue
        try:
            ipaddress.ip_address(server)
        except ValueError:
            hostname = server.removesuffix(".")
            if len(hostname) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in hostname.split(".")
            ):
                raise TimeConfigError(
                    f"Invalid time server on line {number}; use one hostname or IP address per line."
                ) from None
        if server not in servers:
            servers.append(server)
    return tuple(servers) or DEFAULT_SERVERS


def write_config(servers: tuple[str, ...], output: Path = DEFAULT_OUTPUT) -> None:
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    output.write_text(
        "[Time]\nNTP=\nNTP=" + " ".join(servers) + "\nFallbackNTP=\n",
        encoding="utf-8",
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true", help="Validate configuration only.")
    options = parser.parse_args(arguments)
    try:
        servers = read_servers(options.config)
        if options.check:
            print(f"Valid time servers in {options.config}: {' '.join(servers)}")
            return 0
        write_config(servers)
        print(f"Time servers configured: {' '.join(servers)}", flush=True)
        return 0
    except (TimeConfigError, OSError) as error:
        print(f"Time server setup failed: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())