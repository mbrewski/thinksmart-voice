#!/usr/bin/env bash
# Install the ThinkSmart kiosk onto this panel. Run as root on the device.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/usr/local/share/thinksmart-kiosk
USER_NAME="${KIOSK_USER:-ubuntu}"
UID_NUM="$(id -u "$USER_NAME")"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install-panel.sh must run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  cage python3-gi python3-gi-cairo python3-venv python3-websockets \
  gir1.2-gtk-4.0 gir1.2-webkit-6.0 libwebkitgtk-6.0-4 \
  alsa-utils pipewire pipewire-pulse wireplumber \
  libasound2-plugins pulseaudio-utils cron kmod

install -d -m 755 "$DEST" "$DEST/faces" "$DEST/faces/blink" "$DEST/kws"
install -m 755 "$SRC/thinksmart-face.py" "$DEST/thinksmart-face.py"
install -m 644 "$SRC/thinksmart_voice.py" "$DEST/thinksmart_voice.py"
install -m 644 "$SRC/audioop_compat.py" "$DEST/audioop_compat.py"
install -m 644 "$SRC/instructions.md" "$DEST/instructions.md"
install -m 644 "$SRC/thinksmart-kiosk.html" "$DEST/thinksmart-kiosk.html"
install -m 644 "$SRC/thinksmart-kiosk.js" "$DEST/thinksmart-kiosk.js"
install -m 644 "$SRC/thinksmart_wakeword.py" "$DEST/thinksmart_wakeword.py"
install -m 755 "$SRC/thinksmart-wake.py" "$DEST/thinksmart-wake.py"
install -m 755 "$SRC/thinksmart-watchdog.sh" "$DEST/thinksmart-watchdog.sh"
install -m 644 "$SRC/thinksmart-kiosk.cron" /etc/cron.d/thinksmart-kiosk
install -m 644 "$SRC/bloub-LICENSE.txt" "$DEST/bloub-LICENSE.txt"
install -m 644 "$SRC/kiosk.env.example" "$DEST/kiosk.env.example"
install -m 644 "$SRC/kws/hey_grok.txt" "$DEST/kws/hey_grok.txt"
install -m 644 "$SRC/faces/"*.jpg "$DEST/faces/"
install -m 644 "$SRC/faces/blink/"*.jpg "$DEST/faces/blink/"

if [[ -f "$SRC/kiosk.env" ]]; then
  install -m 600 "$SRC/kiosk.env" "$DEST/kiosk.env"
  install -m 600 "$SRC/kiosk.env" /etc/thinksmart-kiosk.env
elif [[ ! -f /etc/thinksmart-kiosk.env ]]; then
  install -m 600 "$SRC/kiosk.env.example" /etc/thinksmart-kiosk.env
  echo "wrote /etc/thinksmart-kiosk.env from example; set XAI_API_KEY" >&2
fi

if [[ ! -x "$DEST/venv/bin/python" ]]; then
  python3 -m venv "$DEST/venv"
fi
chown -R "$USER_NAME:$USER_NAME" "$DEST"

install -m 755 "$SRC/speaker-route.sh" /usr/local/bin/speaker-route.sh
install -m 755 "$SRC/mic-unmute.sh" /usr/local/bin/mic-unmute.sh
install -m 755 "$SRC/thinksmart-volume.py" /usr/local/bin/thinksmart-volume.py
install -m 644 "$SRC/speaker-route.service" /etc/systemd/system/speaker-route.service
install -m 644 "$SRC/thinksmart-volume.service" /etc/systemd/system/thinksmart-volume.service
install -m 644 "$SRC/thinksmart-wake.service" /etc/systemd/system/thinksmart-wake.service
install -m 644 "$SRC/thinksmart-kiosk.desktop" /usr/share/wayland-sessions/thinksmart-kiosk.desktop

install -d -m 755 /etc/wireplumber/main.lua.d
install -m 644 "$SRC/99-cd18781y-alsa.lua" /etc/wireplumber/main.lua.d/99-cd18781y-alsa.lua

usermod -aG audio,input "$USER_NAME"

install -d -m 755 /etc/sddm.conf.d
cat > /etc/sddm.conf.d/99-thinksmart-autologin.conf << EOF
[Autologin]
User=$USER_NAME
Session=thinksmart-kiosk.desktop
EOF
if [[ -f /etc/sddm.conf ]]; then
  if grep -q '^Session=' /etc/sddm.conf; then
    sed -i 's/^Session=.*/Session=thinksmart-kiosk.desktop/' /etc/sddm.conf
  else
    printf '\n[Autologin]\nSession=thinksmart-kiosk.desktop\n' >> /etc/sddm.conf
  fi
fi

systemctl mask pmos-audio-route.service i2c1-pinctrl-fix.service idle-blanker.service 2>/dev/null || true
ln -sfn /dev/null /etc/systemd/system/pmos-audio-route.service
ln -sfn /dev/null /etc/systemd/system/i2c1-pinctrl-fix.service
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/.config/systemd/user" \
  "$USER_HOME/.config/systemd/user/graphical-session.target.wants"
ln -sfn /dev/null "$USER_HOME/.config/systemd/user/idle-blanker.service"
rm -f "$USER_HOME/.config/systemd/user/graphical-session.target.wants/idle-blanker.service"
chown -h "$USER_NAME:$USER_NAME" "$USER_HOME/.config/systemd/user/idle-blanker.service" 2>/dev/null || true
if [[ -d /run/user/$UID_NUM ]]; then
  sudo -u "$USER_NAME" XDG_RUNTIME_DIR="/run/user/$UID_NUM" \
    systemctl --user mask idle-blanker.service 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable speaker-route.service thinksmart-volume.service thinksmart-wake.service cron
systemctl restart speaker-route.service thinksmart-volume.service thinksmart-wake.service || true
systemctl restart cron || true

if [[ -d /run/user/$UID_NUM ]]; then
  sudo -u "$USER_NAME" XDG_RUNTIME_DIR="/run/user/$UID_NUM" \
    systemctl --user restart wireplumber pipewire pipewire-pulse 2>/dev/null || true
fi

pkill -f "^python3 $DEST/thinksmart-face.py" 2>/dev/null || true

echo "panel kiosk installed at $DEST"
echo "sddm session: thinksmart-kiosk.desktop (cage). next login drops Plasma."
