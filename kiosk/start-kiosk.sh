#!/bin/sh
# Start the kiosk again after exit-to-terminal.sh.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -n "$0" "$@"
fi

systemctl restart sddm.service
systemctl start thinksmart-wake.service
chvt 1
