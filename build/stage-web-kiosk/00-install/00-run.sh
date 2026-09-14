#!/bin/bash
set -euo pipefail

install -d -m 0755 "${ROOTFS_DIR:?}/opt/web-kiosk-installer"
cp -a files/. "$ROOTFS_DIR/opt/web-kiosk-installer/"

on_chroot <<'EOF'
bash /opt/web-kiosk-installer/scripts/install-kiosk.sh --image
EOF

rm -r -- "$ROOTFS_DIR/opt/web-kiosk-installer"