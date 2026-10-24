#!/bin/sh
# Sets up the Wi-Fi Surveyor satellite on a Raspberry Pi (Raspberry Pi OS) or any Debian/Ubuntu
# machine: pyserial, serial-port access, and a service that starts it at boot as this user.
# Run it as the user who'll own it (not root):   sh install.sh
set -e
if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user; it asks for sudo when it needs it." >&2
    exit 1
fi
here=$(cd "$(dirname "$0")" && pwd)
target=/opt/wifisurveyor-satellite
user=$(id -un)

sudo apt-get install -y python3 python3-serial
sudo usermod -aG dialout "$user"
sudo mkdir -p "$target"
sudo install -m 755 "$here/wifisurveyor_satellite.py" "$target/wifisurveyor_satellite.py"

sudo tee /etc/systemd/system/wifisurveyor-satellite.service >/dev/null <<UNIT
[Unit]
Description=Wi-Fi Surveyor satellite (tinySA over the LAN)
After=network-online.target
Wants=network-online.target

[Service]
User=$user
Group=dialout
ExecStart=/usr/bin/python3 $target/wifisurveyor_satellite.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now wifisurveyor-satellite
# Let the app find it and talk to it, if a firewall is on
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
    sudo ufw allow 8737/tcp
    sudo ufw allow 8737/udp
fi
sleep 1
echo
echo "Installed. In Wi-Fi Surveyor: Frequencies -> Satellites -> Find, then enter this code:"
sudo -u "$user" /usr/bin/python3 "$target/wifisurveyor_satellite.py" --show-code
