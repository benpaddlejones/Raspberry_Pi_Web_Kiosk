#!/bin/bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
version=""
check_only=false

fail() {
    printf 'Image build: %s\n' "$*" >&2
    exit 1
}

while (( $# )); do
    case "$1" in
        --version)
            (( $# >= 2 )) || fail '--version requires a value.'
            version="$2"
            shift 2
            ;;
        --check) check_only=true; shift ;;
        --help)
            printf '%s\n' \
                'Usage: sudo bash scripts/build-image.sh [--version VERSION] [--check]' \
                'Requires native ARM64 Debian/Ubuntu, build dependencies and 20 GiB free.' \
                'The Build and Release workflow provisions a suitable runner.'
            exit 0
            ;;
        *) fail "Unknown option: $1" ;;
    esac
done

cd "$project_root"
settings="$(python3 -B -c \
    'import sys; from scripts.release import read_config; config = read_config(version=sys.argv[1]); print(config["version"]); print(config["build"]["pi_gen_revision"])' \
    "$version")"
mapfile -t values <<< "$settings"
version="${values[0]}"
pi_gen_revision="${values[1]}"

[[ "$(uname -m)" == aarch64 ]] || fail 'Use a native ARM64 host or the GitHub ARM64 release runner.'
(( EUID == 0 )) || fail 'Run the image build with sudo on a dedicated build host.'
[[ "$project_root" != *[[:space:]]* ]] || fail 'pi-gen requires a workspace path without spaces.'
for tool in git debootstrap quilt parted losetup mkfs.vfat mkfs.ext4 rsync xz curl xxd bmaptool; do
    command -v "$tool" >/dev/null 2>&1 || fail "Missing build dependency: $tool"
done
available_kb="$(df --output=avail "$project_root" | tail -n 1)"
(( available_kb >= 20 * 1024 * 1024 )) || fail 'At least 20 GiB of free disk space is required.'
losetup --find >/dev/null || fail 'Loop devices are unavailable; use a privileged build host.'
if "$check_only"; then
    printf '%s\n' 'Image build prerequisites are available.'
    exit 0
fi

shopt -s nullglob
existing_images=("$project_root"/deploy/*.img)
(( ${#existing_images[@]} == 0 )) || fail 'Move existing images out of deploy before building again.'
install -d "$project_root/build/work" "$project_root/deploy"
run_dir="$(mktemp -d "$project_root/build/work/run.XXXXXX")"
pi_gen_dir="$run_dir/pi-gen"
printf 'Build workspace: %s\n' "$run_dir"

git init --quiet "$pi_gen_dir"
git -C "$pi_gen_dir" remote add origin https://github.com/RPi-Distro/pi-gen.git
git -C "$pi_gen_dir" fetch --depth=1 origin "$pi_gen_revision"
git -C "$pi_gen_dir" checkout --detach FETCH_HEAD
cp "$project_root/build/config" "$pi_gen_dir/config"
first_user_pass="$(head -c 32 /dev/urandom | xxd -p -c 64)"
printf '\nIMG_NAME=%q\nWORK_DIR=%q\nDEPLOY_DIR=%q\nFIRST_USER_PASS=%q\n' \
    "RaspberryPi_Web_Kiosk_v$version" "$run_dir/work" "$project_root/deploy" \
    "$first_user_pass" >> "$pi_gen_dir/config"
cp -a "$project_root/build/stage-web-kiosk" "$pi_gen_dir/stage-web-kiosk"
touch "$pi_gen_dir/stage2/SKIP_IMAGES"

payload="$pi_gen_dir/stage-web-kiosk/00-install/files"
install -d "$payload/scripts" "$payload/build/stage-web-kiosk/00-install"
cp "$project_root/kiosk.py" "$project_root/kiosk-url.txt" \
    "$project_root/kiosk_wifi.py" "$project_root/kiosk_wifi_setup.py" "$project_root/kiosk-wifi.txt" \
    "$project_root/kiosk_portal.py" "$project_root/kiosk-portal.txt" \
    "$project_root/THIRD_PARTY_NOTICES.md" "$payload/"
cp "$project_root/scripts/install-kiosk.sh" "$payload/scripts/"
cp -a "$project_root/system" "$payload/system"
cp "$project_root/build/stage-web-kiosk/00-install/00-packages" \
    "$payload/build/stage-web-kiosk/00-install/00-packages"
find "$pi_gen_dir/stage-web-kiosk" -type f -name '*.sh' -exec chmod 0755 {} +

source_commit="$(git -c safe.directory="$project_root" rev-parse HEAD)"
printf 'Project: Raspberry Pi Web Kiosk\nVersion: %s\nSource commit: %s\npi-gen commit: %s\nOS: Raspberry Pi OS, Trixie, arm64\nBuild date: %s\n' \
    "$version" "$source_commit" "$pi_gen_revision" "$(date -u +%FT%TZ)" \
    > "$project_root/deploy/build-manifest.txt"

cd "$pi_gen_dir"
bash build.sh
printf '\nImage build complete. Output: %s/deploy\n' "$project_root"