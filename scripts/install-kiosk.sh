#!/bin/bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
image_build=false

fail() {
    printf 'Installation failed: %s\n' "$*" >&2
    exit 1
}

if (( $# > 1 )); then
    fail 'Usage: install-kiosk.sh [--image | --help]'
fi

case "${1:-}" in
    --help)
        printf '%s\n' \
            'Usage: sudo bash scripts/install-kiosk.sh' \
            'Installs on Raspberry Pi OS Lite (64-bit), Trixie.' \
            '--image is reserved for the pi-gen build stage.'
        exit 0
        ;;
    --image) image_build=true ;;
    '') ;;
    *) fail "Unknown option: $1" ;;
esac

(( EUID == 0 )) || fail 'Run this installer as root on the Raspberry Pi.'
[[ -f /etc/rpi-issue || "$image_build" == true ]] || fail 'This installer requires Raspberry Pi OS.'
[[ "$(dpkg --print-architecture)" == arm64 ]] || fail 'Use Raspberry Pi OS (64-bit).'
source /etc/os-release
[[ "${VERSION_CODENAME:-}" == trixie ]] || fail 'Use the current stable Trixie release.'

if ! "$image_build"; then
    [[ "$(findmnt --noheadings --output FSTYPE --mountpoint /boot/firmware)" == vfat ]] \
        || fail '/boot/firmware must be the mounted FAT boot partition.'
fi

if id kiosk >/dev/null 2>&1 && [[ ! -f /etc/raspberry-pi-web-kiosk/managed-user ]]; then
    fail 'The kiosk username already exists. Use a different administrator username.'
fi

if [[ -e /boot/firmware/kiosk-url.txt ]]; then
    python3 "$project_root/kiosk.py" --config /boot/firmware/kiosk-url.txt --check
else
    python3 "$project_root/kiosk.py" --config "$project_root/kiosk-url.txt" --check
fi
if [[ -e /boot/firmware/kiosk-wifi.txt ]]; then
    python3 "$project_root/kiosk_wifi.py" --config /boot/firmware/kiosk-wifi.txt --check
else
    python3 "$project_root/kiosk_wifi.py" --config "$project_root/kiosk-wifi.txt" --check
fi
if [[ -e /boot/firmware/kiosk-portal.txt ]]; then
    python3 "$project_root/kiosk_portal.py" --config /boot/firmware/kiosk-portal.txt --check
else
    python3 "$project_root/kiosk_portal.py" --config "$project_root/kiosk-portal.txt" --check
fi
if [[ -e /boot/firmware/kiosk-time.txt ]]; then
    python3 "$project_root/kiosk_time.py" --config /boot/firmware/kiosk-time.txt --check
else
    python3 "$project_root/kiosk_time.py" --config "$project_root/kiosk-time.txt" --check
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get full-upgrade --yes
if ! "$image_build"; then
    mapfile -t packages < "$project_root/build/stage-web-kiosk/00-install/00-packages"
    apt-get install --yes --no-install-recommends "${packages[@]}"
fi

install -d -m 0755 /etc/raspberry-pi-web-kiosk
if ! id kiosk >/dev/null 2>&1; then
    useradd --create-home --user-group --shell /bin/bash \
        --groups audio,video,render,input kiosk
    touch /etc/raspberry-pi-web-kiosk/managed-user
fi
passwd --lock kiosk

install -D -m 0755 "$project_root/kiosk.py" /usr/local/lib/raspberry-pi-web-kiosk/kiosk.py
install -m 0755 "$project_root/kiosk_wifi.py" /usr/local/lib/raspberry-pi-web-kiosk/kiosk_wifi.py
install -m 0755 "$project_root/kiosk_wifi_setup.py" \
    /usr/local/lib/raspberry-pi-web-kiosk/kiosk_wifi_setup.py
install -m 0644 "$project_root/system/web-kiosk-wifi.service" \
    /etc/systemd/system/web-kiosk-wifi.service
install -m 0755 "$project_root/kiosk_portal.py" /usr/local/lib/raspberry-pi-web-kiosk/kiosk_portal.py
install -m 0644 "$project_root/system/web-kiosk-portal.service" \
    /etc/systemd/system/web-kiosk-portal.service
install -m 0755 "$project_root/kiosk_time.py" /usr/local/lib/raspberry-pi-web-kiosk/kiosk_time.py
install -m 0644 "$project_root/system/web-kiosk-time.service" \
    /etc/systemd/system/web-kiosk-time.service
install -D -m 0644 "$project_root/THIRD_PARTY_NOTICES.md" \
    /usr/share/doc/raspberry-pi-web-kiosk/THIRD_PARTY_NOTICES.md
install -m 0755 "$project_root/system/web-kiosk-session" /usr/local/bin/web-kiosk-session
install -m 0755 "$project_root/system/web-kiosk-network-setup" /usr/local/bin/web-kiosk-network-setup
install -m 0755 "$project_root/system/web-kiosk-log" /usr/local/bin/web-kiosk-log
install -m 0644 "$project_root/system/web-kiosk-log.service" \
    /etc/systemd/system/web-kiosk-log.service
install -m 0644 "$project_root/system/web-kiosk-log.timer" \
    /etc/systemd/system/web-kiosk-log.timer
install -m 0644 "$project_root/system/web-kiosk-network-setup.service" \
    /etc/systemd/system/web-kiosk-network-setup.service
install -m 0644 -o kiosk -g kiosk "$project_root/system/kiosk.bash_profile" /home/kiosk/.bash_profile
install -D -m 0644 "$project_root/system/getty-autologin.conf" \
    /etc/systemd/system/getty@tty1.service.d/zz-web-kiosk.conf
install -D -m 0644 "$project_root/system/chromium-policy.json" \
    /etc/chromium/policies/managed/web-kiosk.json
install -D -m 0644 "$project_root/system/timesyncd.conf" \
    /etc/systemd/timesyncd.conf.d/zz-web-kiosk.conf
install -m 0644 "$project_root/system/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades
install -m 0644 "$project_root/system/52web-kiosk-unattended-upgrades" \
    /etc/apt/apt.conf.d/52web-kiosk-unattended-upgrades

if [[ ! -e /boot/firmware/kiosk-url.txt ]]; then
    install -m 0644 "$project_root/kiosk-url.txt" /boot/firmware/kiosk-url.txt
fi
if [[ ! -e /boot/firmware/kiosk-wifi.txt ]]; then
    install -m 0644 "$project_root/kiosk-wifi.txt" /boot/firmware/kiosk-wifi.txt
fi
if [[ ! -e /boot/firmware/kiosk-portal.txt ]]; then
    install -m 0644 "$project_root/kiosk-portal.txt" /boot/firmware/kiosk-portal.txt
fi
if [[ ! -e /boot/firmware/kiosk-time.txt ]]; then
    install -m 0644 "$project_root/kiosk-time.txt" /boot/firmware/kiosk-time.txt
fi

systemctl set-default multi-user.target
systemctl enable getty@tty1.service NetworkManager-wait-online.service systemd-timesyncd.service \
    web-kiosk-wifi.service web-kiosk-portal.service web-kiosk-time.service web-kiosk-network-setup.service \
    web-kiosk-log.timer apt-daily.timer apt-daily-upgrade.timer
if ! "$image_build"; then
    systemctl daemon-reload
fi

printf '%s\n' \
    'Kiosk installed. Edit kiosk-url.txt (and optionally kiosk-wifi.txt) on the boot partition, then reboot.' \
    'Daily stable/security updates are enabled; automatic reboots are disabled.'