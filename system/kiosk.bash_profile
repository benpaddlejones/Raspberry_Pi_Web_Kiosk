if [[ "$(tty)" == /dev/tty1 ]]; then
    exec /usr/local/bin/web-kiosk-session
fi