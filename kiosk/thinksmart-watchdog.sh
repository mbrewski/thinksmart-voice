#!/bin/bash
# Clear a stuck kiosk-busy file and restart wake if the mic is idle with no capture.
set -euo pipefail
BUSY=/tmp/kiosk-busy
STALE=45
LOGTAG=thinksmart-watchdog

busy_stale=0
if [[ -f "$BUSY" ]]; then
  now=$(date +%s)
  mtime=$(stat -c %Y "$BUSY")
  age=$((now - mtime))
  if [[ "$age" -gt "$STALE" ]]; then
    rm -f "$BUSY"
    logger -t "$LOGTAG" "cleared stale kiosk-busy age=${age}s"
    busy_stale=1
  fi
fi

if [[ -f "$BUSY" ]]; then
  exit 0
fi

if ! pgrep -f '/thinksmart-wake.py' >/dev/null; then
  systemctl restart thinksmart-wake.service
  logger -t "$LOGTAG" "restarted thinksmart-wake (no process)"
  exit 0
fi

if pgrep -f '/usr/bin/pw-record' >/dev/null; then
  exit 0
fi

state=$(curl -sS -m 2 http://127.0.0.1:1680/state 2>/dev/null || true)
if echo "$state" | grep -qE '"face": "(listen|hold|busy)"'; then
  exit 0
fi

systemctl restart thinksmart-wake.service
logger -t "$LOGTAG" "restarted thinksmart-wake (idle, no pw-record)${busy_stale:+ after stale busy}"
