#!/bin/bash
set -euo pipefail

if (( $# != 2 )); then
    printf '%s\n' 'Usage: generate-checksums.sh VERSION DEPLOY_DIR' >&2
    exit 1
fi
version="$1"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]] || {
    printf '%s\n' 'Invalid release version.' >&2
    exit 1
}
cd -- "$2"
archive="RaspberryPi_Web_Kiosk_v${version}.img.gz"
checksum="RaspberryPi_Web_Kiosk_v${version}.sha256"
[[ -s "$archive" ]] || {
    printf 'Missing image archive: %s\n' "$archive" >&2
    exit 1
}
gzip -t -- "$archive"
sha256sum -- "$archive" > "$checksum"
sha256sum --check "$checksum"
printf 'Raspberry Pi Web Kiosk v%s\n\nLinux: sha256sum -c %s\nmacOS: shasum -a 256 -c %s\nWindows PowerShell: Get-FileHash %s -Algorithm SHA256\n\nCompare the Windows result with the hash in %s.\nFlash the .img.gz directly with Raspberry Pi Imager.\n' \
    "$version" "$checksum" "$checksum" "$archive" "$checksum" \
    > "VERIFY_DOWNLOAD_v${version}.txt"