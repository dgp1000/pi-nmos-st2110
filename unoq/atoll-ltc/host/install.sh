#!/bin/bash
# One-shot setup of the Linux side of the UNO Q for Atoll: island IP on the hub NIC,
# patched linuxptp follower, PTP status service, app autostart. Run ON the UNO Q as
# the arduino user (needs passwordless sudo). Idempotent.
set -euo pipefail
IF="${ATOLL_IFACE:-$(ls /sys/class/net | grep '^enx' | head -1)}"
IP="${ATOLL_IP:-10.10.10.77/24}"
HERE="$(cd "$(dirname "$0")" && pwd)"
echo "== island NIC $IF -> $IP (static, no default route; Wi-Fi keeps SSH/internet)"
nmcli con mod "Wired connection 1" ipv4.method manual ipv4.addresses "$IP" ipv4.gateway "" ipv4.never-default yes connection.autoconnect yes
nmcli con up "Wired connection 1" >/dev/null || true
echo "== packages"
sudo apt-get -qq install -y linuxptp tcpdump ethtool build-essential git >/dev/null
echo "== patched ptp4l (hub NIC has RX-only software timestamps)"
if [ ! -x /usr/local/sbin/ptp4l ]; then
  rm -rf ~/linuxptp && git clone -q --depth 1 --branch v4.2 https://github.com/richardcochran/linuxptp.git ~/linuxptp
  (cd ~/linuxptp && git apply "$HERE/linuxptp-rx-only-timestamps.patch" && make -j4 ptp4l pmc >/dev/null && sudo install -m755 ptp4l pmc /usr/local/sbin/)
fi
echo "== NTP off (it fights ptp4l)"
sudo timedatectl set-ntp false; sudo systemctl disable --now systemd-timesyncd >/dev/null 2>&1 || true
echo "== services"
sudo mkdir -p /etc/linuxptp ~/atoll
sudo install -m644 "$HERE/atoll-follower.cfg" /etc/linuxptp/
sed "s/enx[0-9a-f]*/$IF/g" "$HERE/atoll-ptp.service" | sudo tee /etc/systemd/system/atoll-ptp.service >/dev/null
sudo install -m644 "$HERE/atoll-ptp-status.service" "$HERE/atoll-ltc-autostart.service" /etc/systemd/system/
install -m755 "$HERE/atoll-ptp-status.py" "$HERE/raw-offset.py" "$HERE/usb-latency-test.sh" ~/atoll/
sudo chown -R arduino:arduino ~/atoll
sudo systemctl daemon-reload
sudo systemctl enable --now atoll-ptp atoll-ptp-status >/dev/null
sudo systemctl enable atoll-ltc-autostart >/dev/null
echo "== app: copy unoq/atoll-ltc/{app.yaml,sketch,python} to ~/ArduinoApps/atoll-ltc and run:"
echo "   cd ~/ArduinoApps && arduino-app-cli app start ./atoll-ltc"
echo "check: sudo /usr/local/sbin/pmc -u -b 0 'GET PORT_DATA_SET'   (portState SLAVE after ~2 min)"
