# Build and Release

## Release a Build

The [Build and Release workflow](.github/workflows/build-release.yml) uses pi-gen
to build a Raspberry Pi OS image, compresses it, creates checksums, retains build
logs, and publishes a GitHub Release. HDMI applications, test media, and the old
QEMU boot-test kernel are not included.

1. Commit and push the project files to GitHub.
2. Open **Actions > Build and Release > Run workflow**.
3. Leave the version blank to use the configured version in
	 [.github/release-config.json](.github/release-config.json), or enter a new
	 version without a leading `v`.
4. Download the `.img.gz` and `.sha256` files from the GitHub Release.

The workflow uses a native `ubuntu-24.04-arm` runner. It installs current Trixie
packages from the official repositories and pins pi-gen to the commit in the
release configuration. Before publishing, it validates the image's FAT boot
partition, configuration, installed browser, account permissions, and startup
files. Logs are retained as Actions artifacts even when a build fails. An
existing release version is not overwritten.

The configured release is published as the latest stable version. For a
pre-release, use a version suffix such as `1.1.0-alpha.1` and set `prerelease` to
`true` in the release configuration before running the workflow.

## Source Checks

Python uses only the standard library. Run source and packaging tests with:

```bash
bash scripts/check.sh
```

The check script also runs ShellCheck and actionlint when installed; the release
workflow installs both. Tests cover Windows text files, URL validation, safe
browser arguments, startup retries, Wi-Fi settings parsing and secret removal,
release metadata, and checksum verification.

## Local Image Build

Use a dedicated native ARM64 Debian/Ubuntu host with root privileges, loop-device
support, and at least 20 GiB free. Install the build dependencies listed in the
workflow, then run:

```bash
sudo bash scripts/build-image.sh --check
sudo bash scripts/build-image.sh
```

Output is placed in `deploy/`. Each build uses a fresh directory under
`build/work/`; no existing build directory is deleted. Move any previous `.img`
out of `deploy/` before rebuilding. Ordinary x86-64 Codespaces can run source
checks but are not the supported image-build host. Update the pinned pi-gen
revision deliberately when adopting upstream build changes.

## Hardware Validation

Before promoting a release, test on a physical target Pi:

- unattended first boot without Imager customisation, and Sydney time;
- cold boot, automatic login, and full-screen HDMI output;
- a URL change made from a Windows/macOS computer;
- guided on-screen setup with personal and PEAP Wi-Fi, a wrong password, and a
	 network with a web login page;
- changing an existing Wi-Fi connection with wrong credentials and confirming
	 the previous profile, certificates, and connection are recovered;
- cancelling and confirming Wi-Fi reset, preserving Ethernet/VPN profiles, and
	 quarantining a rejected Wi-Fi profile without exposing its credentials;
- automatic sign-in to the login page, including a wrong password and an expired
	 firewall login;
- WPA2 Personal and Enterprise (PEAP) Wi-Fi from `kiosk-wifi.txt`;
- delayed networking, browser restart, and an OS upgrade followed by reboot.

Image filesystem validation does not emulate the GPU or prove that a physical Pi
boots successfully.