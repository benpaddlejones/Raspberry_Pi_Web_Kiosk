#!/bin/bash
set -euo pipefail

fail() {
    printf 'Image validation failed: %s\n' "$*" >&2
    exit 1
}

if [[ "${1:-}" == --help ]]; then
    printf '%s\n' 'Usage: sudo bash scripts/validate-image.sh path/to/web-kiosk.img'
    exit 0
fi
(( $# == 1 )) || fail 'Pass exactly one image file.'
(( EUID == 0 )) || fail 'Read-only image mounting requires root.'
[[ -f "$1" ]] || fail 'Image file does not exist.'
image="$(realpath "$1")"
mount_dir="$(mktemp -d /tmp/web-kiosk-validate.XXXXXX)"
loop_device=""

cleanup() {
    local status=$?
    trap - EXIT
    if mountpoint -q "$mount_dir/boot"; then
        umount "$mount_dir/boot" || status=1
    fi
    if mountpoint -q "$mount_dir/root"; then
        umount "$mount_dir/root" || status=1
    fi
    if [[ -n "$loop_device" ]]; then
        losetup --detach "$loop_device" || status=1
    fi
    rmdir "$mount_dir/boot" "$mount_dir/root" "$mount_dir" || status=1
    exit "$status"
}

mkdir "$mount_dir/boot" "$mount_dir/root"
trap cleanup EXIT
loop_device="$(losetup --find --show --read-only --partscan "$image")"
[[ "$(blkid -s TYPE -o value "${loop_device}p1")" == vfat ]] || fail 'Boot partition is not FAT.'
[[ "$(blkid -s TYPE -o value "${loop_device}p2")" == ext4 ]] || fail 'Root partition is not ext4.'
mount -o ro "${loop_device}p1" "$mount_dir/boot"
mount -o ro,noload "${loop_device}p2" "$mount_dir/root"
rootfs="$mount_dir/root"

grep -qx 'VERSION_CODENAME=trixie' "$rootfs/etc/os-release" || fail 'Unexpected OS release.'
[[ -s "$mount_dir/boot/config.txt" && -s "$mount_dir/boot/cmdline.txt" ]] \
    || fail 'Raspberry Pi boot configuration is missing.'
python3 -B "$rootfs/usr/local/lib/raspberry-pi-web-kiosk/kiosk.py" \
    --config "$mount_dir/boot/kiosk-url.txt" --check
python3 -B "$rootfs/usr/local/lib/raspberry-pi-web-kiosk/kiosk_wifi.py" \
    --config "$mount_dir/boot/kiosk-wifi.txt" --check
[[ -f "$mount_dir/boot/kiosk-wifi.txt" ]] || fail 'The Wi-Fi settings template is missing.'
[[ -L "$rootfs/etc/systemd/system/multi-user.target.wants/web-kiosk-wifi.service" ]] \
    || fail 'Wi-Fi provisioning is not enabled.'
python3 -B "$rootfs/usr/local/lib/raspberry-pi-web-kiosk/kiosk_portal.py" \
    --config "$mount_dir/boot/kiosk-portal.txt" --check
[[ -f "$mount_dir/boot/kiosk-portal.txt" ]] || fail 'The portal sign-in settings are missing.'
[[ -L "$rootfs/etc/systemd/system/multi-user.target.wants/web-kiosk-portal.service" ]] \
    || fail 'Portal sign-in is not enabled.'
[[ -L "$rootfs/etc/systemd/system/timers.target.wants/web-kiosk-log.timer" \
    && -x "$rootfs/usr/local/bin/web-kiosk-log" ]] \
    || fail 'The boot partition log is not enabled.'
[[ -L "$rootfs/etc/systemd/system/multi-user.target.wants/web-kiosk-network-setup.service" \
    && -x "$rootfs/usr/local/bin/web-kiosk-network-setup" && -x "$rootfs/usr/bin/nmtui" \
    && -x "$rootfs/usr/bin/whiptail" \
    && -f "$rootfs/usr/local/lib/raspberry-pi-web-kiosk/kiosk_wifi_setup.py" ]] \
    || fail 'The network setup screen is not installed.'
[[ "$(readlink "$rootfs/etc/localtime")" == */Australia/Sydney ]] \
    || fail 'The timezone is not Australia/Sydney.'
grep -qx 'NTP=time.windows.com' "$rootfs/etc/systemd/timesyncd.conf.d/zz-web-kiosk.conf" \
    || fail 'The Microsoft time server default is not configured.'
[[ -f "$mount_dir/boot/kiosk-time.txt" ]] || fail 'The time server settings are missing.'
python3 -B "$rootfs/usr/local/lib/raspberry-pi-web-kiosk/kiosk_time.py" \
    --config "$mount_dir/boot/kiosk-time.txt" --check
[[ -L "$rootfs/etc/systemd/system/sysinit.target.wants/web-kiosk-time.service" ]] \
    || fail 'Time server configuration is not enabled.'
awk -F: '$1 == "kioskadmin" { locked = ($2 ~ /^[!*]/) } END { exit !locked }' \
    "$rootfs/etc/shadow" || fail 'The administrator account is not locked.'
[[ -x "$rootfs/usr/bin/chromium" && -x "$rootfs/usr/bin/cage" ]] \
    || fail 'The browser or compositor is missing.'
[[ -x "$rootfs/usr/local/bin/web-kiosk-session" ]] || fail 'The kiosk session is missing.'
grep -q -- '--autologin kiosk' \
    "$rootfs/etc/systemd/system/getty@tty1.service.d/zz-web-kiosk.conf" \
    || fail 'Automatic kiosk login is not configured.'
grep -q '/usr/local/bin/web-kiosk-session' "$rootfs/home/kiosk/.bash_profile" \
    || fail 'The login session does not start the kiosk.'
[[ "$(readlink "$rootfs/etc/systemd/system/default.target")" == */multi-user.target ]] \
    || fail 'The default boot target is not the kiosk console.'
awk -F: '$1 == "kiosk" { found = ($3 >= 1000 && $7 == "/bin/bash") } END { exit !found }' \
    "$rootfs/etc/passwd" || fail 'The unprivileged kiosk user is missing.'
awk -F: '$1 == "kiosk" { locked = ($2 ~ /^[!*]/) } END { exit !locked }' \
    "$rootfs/etc/shadow" || fail 'The kiosk password is not locked.'
if awk -F: '$1 == "sudo" && $4 ~ /(^|,)kiosk(,|$)/ { found = 1 } END { exit !found }' \
    "$rootfs/etc/group"; then
    fail 'The kiosk user must not be an administrator.'
fi
[[ -s "$rootfs/etc/apt/apt.conf.d/52web-kiosk-unattended-upgrades" ]] \
    || fail 'Automatic OS updates are not configured.'
dpkg-query --admindir="$rootfs/var/lib/dpkg" --show \
    --showformat='${Package} ${Version} ${Architecture} ${db:Status-Status}\n' \
    chromium cage unattended-upgrades
printf '%s\n' 'Image filesystem validation passed. Physical Pi boot testing is still required.'