#!/usr/bin/env bash
# Install thermallcd: udev rule + systemd service for the Trofeo Vision LCD.
# Works on any USB port: the udev rule matches VID:PID and starts the service
# whenever the panel is plugged in; the monitor also retries on its own.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo cp "$DIR/99-thermallcd.rules" /etc/udev/rules.d/
sed -e "s|@USER@|$(id -un)|" -e "s|@REPO@|$DIR|" "$DIR/thermallcd.service.template" | sudo tee /etc/systemd/system/thermallcd.service >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --attr-match="idVendor=0416" --attr-match="idProduct=5408" || true
sudo systemctl daemon-reload
sudo systemctl enable --now thermallcd.service
echo
echo "installed. check with:  systemctl status thermallcd.service"
echo "live logs:              journalctl -u thermallcd.service -f"
