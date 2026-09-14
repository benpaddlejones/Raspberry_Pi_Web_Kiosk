# Raspberry Pi Web Kiosk

A Raspberry Pi that automatically logs in and opens a configured website in a
full-screen Chromium kiosk. Change the website by editing
[kiosk-url.txt](kiosk-url.txt) on the boot drive's Windows/macOS-readable FAT
partition. You do not need to access the Linux partition.

The system uses **Raspberry Pi OS Lite (64-bit), Debian 13 Trixie**, the current
stable Raspberry Pi OS release. Cage provides a dedicated Wayland kiosk session;
Chromium is the Chrome-compatible browser packaged for Raspberry Pi. There is no
desktop panel or application launcher.

**Status:** initial pre-release implementation. Source, packaging and workflow
checks are available, but a flashable image must be built by the release pipeline
and tested on physical Raspberry Pi hardware before deployment.

## Build a Release

The [Build and Release workflow](.github/workflows/build-release.yml) is adapted
from the Raspberry HDMI Tester repository. It retains the manual release process,
JSON version configuration, pi-gen image build, compressed image, checksums, build
logs and GitHub Release publishing. HDMI applications, test media and the old
QEMU boot-test kernel are not included.

1. Commit and push this project's files to GitHub.
2. Open **Actions > Build and Release > Run workflow**.
3. Leave the version blank to use
	 [.github/release-config.json](.github/release-config.json), or enter a new
	 version such as `0.1.0-alpha.2` without a leading `v`.
4. Download the resulting `.img.gz` and `.sha256` from the GitHub Release.

The workflow uses a native `ubuntu-24.04-arm` runner. It installs current Trixie
packages from the official repositories and pins the pi-gen builder to the commit
in the release configuration. It validates the image's FAT boot partition,
configuration, installed browser, account permissions and startup files before
publishing. Logs are retained as Actions artifacts even when a build fails.
An existing release version is not overwritten.

Initial releases are marked as pre-releases because physical boot testing is
still required. Using a stable OS does not make an untested kiosk image a stable
product release. Set `prerelease` to `false` only after hardware validation.

## Flash and Start

Target hardware: Raspberry Pi 4, 400, 5 or 500, an HDMI display, a suitable power
supply and a 16 GB or larger SD card or USB boot drive. At least 2 GB RAM is
recommended. USB boot must already be supported and enabled on your Pi.

1. Verify the downloaded archive against its SHA-256 checksum. The release also
	 includes verification instructions for Windows, macOS and Linux.
2. In [Raspberry Pi Imager](https://www.raspberrypi.com/software/), choose your Pi,
	 then **Use custom** and select the `.img.gz`. No extraction is necessary.
3. In OS customisation, set your own administrator username and password.
	 **Do not use `kiosk`: that name is reserved for the locked browser account.**
	 Set Wi-Fi, country and timezone if needed; Ethernet with DHCP is the default.
	 SSH is disabled unless you explicitly enable it during customisation.
4. Write the image, then unplug and reinsert the boot drive into your computer.
5. Open the readable `bootfs` partition and edit its `kiosk-url.txt` file.
	 Put one complete URL on its own line, for example:

	 ```text
	 https://example.com/dashboard
	 ```

6. Save as plain UTF-8 text, safely eject the drive, and connect it to the Pi.
7. Connect HDMI and the network, then power on. After the normal first-boot
	 provisioning, the Pi logs in as `kiosk` and starts Chromium automatically.

If Imager does not offer OS customisation, configure the administrator using
Raspberry Pi OS's first-boot prompts with a keyboard, or use the existing-OS
installation below. Do not skip account setup for an unattended installation.

If Windows asks to format another partition, **cancel**. That is the Linux root
partition and must not be formatted. The configuration file belongs at the root
of `bootfs`, not on a separate USB stick.

## Change the Website

Shut down the Pi cleanly before moving its SD card or USB boot drive to another
computer. Edit `kiosk-url.txt` on `bootfs`, eject safely, and boot the Pi again.
Windows Notepad's UTF-8 format, with or without a BOM, and Windows line endings
are supported. In TextEdit, use plain text rather than a rich-text document.
Ensure the filename is not accidentally saved as `kiosk-url.txt.txt`.

The file must contain exactly one `http://` or `https://` URL. Local addresses and
ports are supported, for example `http://192.168.1.20:8080/dashboard`. Blank lines
and surrounding spaces are ignored. Browser flags, scripts, file URLs, embedded
username/password pairs and multiple URLs are rejected. The file is read as data,
never executed as a shell script. Do not store secrets in this readable file.

On the Pi, the same file is `/boot/firmware/kiosk-url.txt`. Changes take effect on
the next kiosk start or reboot, not immediately in an already-open browser.

## Install on Existing Raspberry Pi OS

Start with an updated **Raspberry Pi OS Lite (64-bit), Trixie** installation and
configure its administrator and networking with Imager. Run these commands on
the Pi, not in Codespaces or on your everyday desktop:

```bash
sudo apt-get update
sudo apt-get install --yes git
git clone https://github.com/benpaddlejones/Raspberry_Pi_Web_Kiosk.git
cd Raspberry_Pi_Web_Kiosk
sudo bash scripts/install-kiosk.sh
sudo nano /boot/firmware/kiosk-url.txt
sudo reboot
```

The installer updates the OS, installs the kiosk packages, creates a separate
unprivileged account, and configures automatic login on the first console. It
preserves an existing URL file. It deliberately switches the device to a
dedicated kiosk boot session, so use a Pi dedicated to this purpose. An existing
unmanaged account called `kiosk` causes installation to stop instead of changing
that account's privileges or files.

## Operation and Updates

- The browser runs as `kiosk`, never as root, and keeps Chromium's sandbox and TLS
	certificate checks enabled. The kiosk account has a locked password and no
	administrator privileges. There is no shared default administrator password.
- Startup waits until the configured website host's port is reachable. If the
	network is slow or unavailable at boot, it retries every five seconds. This is
	a startup check, not ongoing website health monitoring or proxy support.
- Closing or crashing the browser ends the session; systemd restarts login after
	five seconds and reads the URL again. Chromium retains its browser profile and
	website cookies under the kiosk account, so logins can persist across restarts.
- Cage does not run a desktop screensaver or idle-blanking daemon. A monitor's
	own sleep settings still apply.
- Daily unattended upgrades install stable and security updates from the Debian
	and Raspberry Pi repositories. Major OS releases are not automatically selected.
	Automatic reboots are disabled to avoid interrupting the display.
- Schedule a maintenance reboot to activate kernel changes and restart Chromium
	with its updated binary. For manual maintenance, log in as your administrator:

	```bash
	sudo apt-get update
	sudo apt-get full-upgrade --yes
	sudo reboot
	```

Physical keyboard access can switch to another console with Ctrl+Alt+F2 for
administrator login; Ctrl+Alt+F1 returns to the kiosk. Kiosk mode is not a physical
security boundary. For a public unattended display, restrict access to the
keyboard, boot media and network. To shut down safely:

```bash
sudo poweroff
```

## Troubleshooting

From an administrator console or SSH session, validate the URL and inspect logs:

```bash
python3 /usr/local/lib/raspberry-pi-web-kiosk/kiosk.py --check
journalctl -b -t web-kiosk --no-pager
sudo systemctl status getty@tty1.service
nmcli device status
```

Missing or malformed configuration causes startup to retry and records an error
in the journal. A blank screen with "Website host unavailable" in the logs means
the configured host cannot yet be reached. Check the URL, network and website.
For HTTPS errors, also check the Pi's clock and the site's certificate. A network
failure after the browser has opened is handled by the website/browser; restart
the session to reload it after connectivity is restored:

```bash
sudo systemctl restart getty@tty1.service
```

This terminates the current kiosk session. The command must be run from a
different console or SSH, not from the kiosk account.

## Development and Hardware Validation

Python uses only the standard library. Run source and packaging tests with:

```bash
bash scripts/check.sh
```

The check script also runs ShellCheck and actionlint when installed; the release
workflow installs both. Tests cover Windows text files, URL validation, safe
browser arguments, startup retries, release metadata and checksum verification.

For a local image build, use a dedicated native ARM64 Debian/Ubuntu host with
root privileges, loop-device support and at least 20 GiB free. Install the build
dependencies listed in the workflow, then run:

```bash
sudo bash scripts/build-image.sh --check
sudo bash scripts/build-image.sh
```

Output is placed in `deploy/`. Each build uses a fresh directory under
`build/work/`; no existing build directory is deleted. Move any previous `.img`
out of `deploy/` before rebuilding. Ordinary x86-64 Codespaces can run source
checks but are not the supported image-build host. Update the pinned pi-gen
revision deliberately when adopting upstream build changes.

Before promoting a release, test on a physical target Pi: Imager provisioning,
cold boot, automatic login, full-screen HDMI output, a URL change made from a
Windows/macOS computer, delayed networking, browser restart, and an OS upgrade
followed by reboot. Image filesystem validation does not emulate the GPU or prove
that a physical Pi boots successfully.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the reused components and
their licenses.