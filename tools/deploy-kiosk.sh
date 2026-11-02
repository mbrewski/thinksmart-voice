#!/usr/bin/env bash
# Copy kiosk/ onto the panel over SSH and run install-panel.sh as root.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:?usage: tools/deploy-kiosk.sh ubuntu@PANEL_IPV4}"
STAGE=/tmp/thinksmart-kiosk-src

echo "staging kiosk files on $HOST"
tar -C "$ROOT" \
  --exclude 'kiosk/venv' \
  --exclude 'kiosk/__pycache__' \
  --exclude '*.pyc' \
  -czf - kiosk \
| ssh -o StrictHostKeyChecking=accept-new "$HOST" \
  "rm -rf $STAGE && mkdir -p $STAGE && tar -C $STAGE -xzf -"

echo "installing on $HOST (sudo)"
ssh -t "$HOST" "sudo bash $STAGE/kiosk/install-panel.sh"

echo "deployed. on the panel: curl http://127.0.0.1:1680/state"
