# Raspberry Pi Web Kiosk

A Raspberry Pi that automatically logs in and opens a configured website in a
full-screen Chromium kiosk. Change the website by editing
[kiosk-url.txt](kiosk-url.txt) on the boot drive's Windows/macOS-readable FAT
partition. You do not need to access the Linux partition.

The image is preset for Sydney and needs no setup in Raspberry Pi Imager or at
first boot. Networking uses Ethernet, an on-screen setup at first boot, or
[kiosk-wifi.txt](kiosk-wifi.txt), including enterprise (802.1X) Wi-Fi.

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
3. Skip OS customisation. The image is preset for Sydney (`Australia/Sydney`
	 timezone, `en_AU.UTF-8` locale, US keyboard layout, `AU` Wi-Fi country) and
	 needs no account setup at first boot. SSH is disabled.
4. Write the image, then unplug and reinsert the boot drive into your computer.
5. Open the readable `bootfs` partition and edit its `kiosk-url.txt` file.
	 Put one complete URL on its own line, for example:

	 ```text
	 https://example.com/dashboard
	 ```

6. For Wi-Fi, either connect a USB keyboard to use the on-screen setup at first
	 boot, or edit `kiosk-wifi.txt` (see [Wi-Fi](#wi-fi)). Ethernet with DHCP
	 needs no settings.
7. Save as plain UTF-8 text, safely eject the drive, and connect it to the Pi.
8. Connect HDMI and the network (or a keyboard for Wi-Fi setup), then power on.
	 The Pi logs in as `kiosk` and starts Chromium automatically. The first boot
	 can take up to about a minute longer while the Pi checks for a network.

### Preset Region and Accounts

| Setting | Value |
|---|---|
| Timezone | `Australia/Sydney` |
| Locale | `en_AU.UTF-8` |
| Keyboard | US layout (standard for Australian keyboards) |
| Wi-Fi country | `AU` |
| Time servers | Google: `time1`-`time4.google.com`, fallback `time.google.com` |
| Kiosk account | `kiosk`: unprivileged, locked password, automatic login |
| Administrator | `kioskadmin`: password locked, no login |
| SSH | Disabled |

To build for another region, change `TIMEZONE_DEFAULT`, `LOCALE_DEFAULT`,
`KEYBOARD_KEYMAP`, `KEYBOARD_LAYOUT` and `WPA_COUNTRY` in
[build/config](build/config).

The clock is set by `systemd-timesyncd` from Google Public NTP, configured in
[system/timesyncd-google.conf](system/timesyncd-google.conf). Google smears leap
seconds, so the file uses only Google servers. Networks that block outbound NTP
(UDP port 123) prevent time sync, and HTTPS sites may then fail with certificate
errors.

The administrator password is a random value generated during each build and
then removed, so nobody can log in with it and there is no shared default
password. To maintain or troubleshoot a device, reflash it, or use the
existing-OS installation below on a Pi where you control the administrator
account.

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

## Wi-Fi

There are three ways to connect. Saved connections are kept permanently, across
reboots and OS updates.

| Method | When to use | Keyboard needed |
|---|---|---|
| Ethernet (DHCP) | Wired network | No |
| On-screen setup | Wi-Fi, without putting passwords on the card | Yes, at first boot |
| `kiosk-wifi.txt` | Wi-Fi without a keyboard, WPA3 Enterprise 192-bit, Dynamic WEP or EAP-FAST | No |

At boot, `kiosk-wifi.txt` is applied first. If the Pi is then online, the
on-screen setup is skipped.

### On-screen Setup at First Boot

If the Pi has no network connection at first boot, it shows a network setup
screen before the kiosk starts. Connect a USB keyboard and press Enter. Use the
arrow keys to move, Enter to choose, and Tab to reach the buttons.

1. Choose **Connect to Wi-Fi**. The Pi lists nearby networks with their signal
	and how they sign in (*password* or *username + password*). Choose yours, or
	**Hidden network** to type a name.
2. Enter what it asks for:
	- *Password* networks: the Wi-Fi password.
	- *Username + password* (enterprise) networks: your username, your password,
	  and whether IT provides a CA certificate. Choose **No certificate** if
	  you don't have one. PEAP with MSCHAPv2, the most common setup, is used.
3. The Pi connects and reports the result: connected, wrong username or password,
	network not found, or connected but a web login page is required. Choose
	**Connect to Wi-Fi** again to retry.
4. Choose **Finish and start the kiosk**.

**Advanced network settings** opens NetworkManager's full text tool, `nmtui`,
for anything else (TTLS, TLS, static IP addresses). In `nmtui`, enterprise
networks must be added with **Edit a connection > Add > Wi-Fi**.

Settings are saved permanently in the Pi's protected system storage; nothing is
written to the readable boot partition. Once online, the screen is skipped on
later boots. If no key is pressed within 60 seconds, the kiosk starts anyway, and
the screen appears again on the next boot until a network works. Anyone with a
keyboard can change the network while this screen is shown.

### Settings File

`kiosk-wifi.txt` on `bootfs` offers the same choices as the Raspberry Pi OS
desktop Wi-Fi dialog. Remove the `#` from the lines you need; every option is
described in the file. For example, a WPA2 Enterprise PEAP network that does not
provide a CA certificate:

```text
ssid = SchoolWiFi
security = wpa-enterprise
eap = peap
username = jsmith
password = your-password
ca-cert = none
```

| Security | Settings |
|---|---|
| `none`, `owe` | `ssid`, `hidden` |
| `wpa-personal`, `wpa3-personal` | `password` |
| `wep` | `password`, `wep-key-type`, `wep-key-index`, `wep-auth` |
| `leap` | `username`, `password` |
| `wpa-enterprise`, `dynamic-wep`, `wpa3-enterprise` | `eap` = `peap`, `ttls`, `tls`, `pwd`, `fast` or `leap`, plus `username`, `password`, `anonymous-identity`, `domain`, `ca-cert`, `peap-version`, `inner-auth`, `client-cert`, `private-key`, `private-key-password`, `fast-provisioning`, `pac-file` as relevant |

Certificate and key files are copied next to `kiosk-wifi.txt` on `bootfs` and
named in it, for example `ca-cert = ca.pem`. Use `ca-cert = system` to trust the
OS certificate store, or `ca-cert = none` to skip server verification. `none`
matches the dialog's "No CA certificate is required" option, but it lets a fake
access point capture the password. Prefer a CA certificate and `domain`. WEP,
LEAP and dynamic WEP are insecure and provided only for legacy networks.

On the next boot, a root service validates the file and saves a NetworkManager
connection that only root can read. It then renames the file to
`kiosk-wifi-applied.txt` with the passwords removed and deletes the certificate
and key files from `bootfs`. If the settings are invalid, nothing is changed and
the reason is written to `kiosk-wifi-error.txt`. To change Wi-Fi, create a new
`kiosk-wifi.txt` (for example, from `kiosk-wifi-applied.txt`) and re-enter the
passwords.

Deleted files on a FAT drive can be recovered with forensic tools. Treat a lost
or discarded boot drive that held Wi-Fi credentials as exposed, and change the
password. The on-screen setup avoids this.

### Change Wi-Fi Later

Shut down the Pi and move the boot drive to another computer, then either:

- create an empty `kiosk-network-setup.txt` on `bootfs` to show the on-screen
	setup on the next boot, even when a network is available; or
- create a new `kiosk-wifi.txt`. It replaces the connection previously saved from
	this file. A connection added on screen is kept alongside it.

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
also installs the Wi-Fi settings file and on-screen setup. It preserves existing
`kiosk-url.txt` and `kiosk-wifi.txt` files and does not change your administrator,
locale or timezone, but it does switch time sync to Google's servers. It deliberately switches the device to a
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
- If the network redirects to a web login page (for example the NSW DoE edge
	portal while the firewall is still identifying the device), startup waits up
	to three minutes for the redirect to clear before opening Chromium. Detection
	uses Google's `http://connectivitycheck.gstatic.com/generate_204`; if that is
	unreachable, startup is not delayed. After three minutes the browser opens on
	the login page so someone can sign in.
- Closing or crashing the browser ends the session; systemd restarts login after
	five seconds and reads the URL again. Chromium retains its browser profile and
	website cookies under the kiosk account, so logins can persist across restarts.
- Cage does not run a desktop screensaver or idle-blanking daemon. A monitor's
	own sleep settings still apply.
- Daily unattended upgrades install stable and security updates from the Debian
	and Raspberry Pi repositories. Major OS releases are not automatically selected.
	Automatic reboots are disabled to avoid interrupting the display.
- Schedule a maintenance reboot to activate kernel changes and restart Chromium
	with its updated binary. On existing-OS installations, manual maintenance is
	done as your administrator:

	```bash
	sudo apt-get update
	sudo apt-get full-upgrade --yes
	sudo reboot
	```

Physical keyboard access can switch to another console with Ctrl+Alt+F2 for
administrator login on existing-OS installations; Ctrl+Alt+F1 returns to the
kiosk. Kiosk mode is not a physical security boundary. For a public unattended
display, restrict access to the keyboard, boot media and network. To shut down
safely from an administrator console:

```bash
sudo poweroff
```

## Troubleshooting

The released image has no administrator login. On the image:

- **No network:** create `kiosk-network-setup.txt` on `bootfs`, connect a keyboard
	and reboot to use the on-screen setup.
- **`kiosk-wifi.txt` not applied:** read `kiosk-wifi-error.txt` on `bootfs`, fix
	the file and reboot.
- **Wrong website:** check `kiosk-url.txt` as described above.

On an existing-OS installation, use an administrator console or SSH session to
validate the settings and inspect logs:

```bash
python3 /usr/local/lib/raspberry-pi-web-kiosk/kiosk.py --check
python3 /usr/local/lib/raspberry-pi-web-kiosk/kiosk_wifi.py --check
journalctl -b -t web-kiosk -t web-kiosk-wifi -t web-kiosk-network-setup --no-pager
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
browser arguments, startup retries, Wi-Fi settings parsing and secret removal,
release metadata and checksum verification.

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

Before promoting a release, test on a physical target Pi:

- unattended first boot without Imager customisation, and Sydney time;
- cold boot, automatic login and full-screen HDMI output;
- a URL change made from a Windows/macOS computer;
- the guided on-screen setup with personal and PEAP Wi-Fi, a wrong password,
	and a network with a web login page;
- WPA2 Personal and Enterprise (PEAP) Wi-Fi from `kiosk-wifi.txt`;
- delayed networking, browser restart, and an OS upgrade followed by reboot.

Image filesystem validation does not emulate the GPU or prove that a physical Pi
boots successfully.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the reused components and
their licenses.