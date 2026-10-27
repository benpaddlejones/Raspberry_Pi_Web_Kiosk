#!/bin/bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

python3 -B -m unittest discover -s tests -v
python3 -B kiosk.py --config kiosk-url.txt --check
python3 -B kiosk_wifi.py --config kiosk-wifi.txt --check
python3 -B scripts/release.py metadata

mapfile -d '' scripts < <(find scripts system build/stage-web-kiosk \
    -type f \( -name '*.sh' -o -name 'web-kiosk-session' -o -name 'web-kiosk-network-setup' -o -name '*.bash_profile' \) -print0)
for script in "${scripts[@]}"; do
    bash -n "$script"
done
bash -n build/config
bash -n build/stage-web-kiosk/EXPORT_IMAGE
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck --shell=bash --external-sources --exclude=SC1091 "${scripts[@]}"
else
    printf '%s\n' 'ShellCheck is not installed; shell lint skipped locally.'
fi
if command -v actionlint >/dev/null 2>&1; then
    actionlint
else
    printf '%s\n' 'actionlint is not installed; workflow lint skipped locally.'
fi