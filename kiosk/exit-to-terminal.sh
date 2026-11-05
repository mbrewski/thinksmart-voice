#!/bin/sh
# Stop the kiosk and switch to the tty2 login prompt.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -n "$0" "$@"
fi

session=$(loginctl list-sessions --no-legend | awk '$3 == "ubuntu" && $4 == "seat0" { print $1; exit }')

systemctl --no-block stop sddm.service
if [ -n "$session" ]; then
    loginctl terminate-session "$session" || true
fi

i=0
while pgrep -f '^python3 /usr/local/share/thinksmart-kiosk/thinksmart-face.py' >/dev/null; do
    i=$((i + 1))
    [ "$i" -ge 20 ] && break
    sleep 0.25
done

systemctl stop thinksmart-wake.service || true
systemctl start getty@tty2.service || true
chvt 2
