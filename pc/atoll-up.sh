#!/usr/bin/env bash
# Bring up the whole Atoll rig on a Linux-NATIVE host — the cross-platform sibling of restore.ps1.
# (On WSL2, prefer restore.ps1: it also handles the VM keepalive + the D3D12 GPU driver.)
#   bash pc/atoll-up.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"
[ "${ATOLL_PLATFORM:-}" = wsl ] && echo "note: this is the Linux-native launcher; on WSL use restore.ps1" >&2

echo "[1/4] NMOS stack (docker: registry :8080 / virtnode :8090 / testing :5000)..."
if command -v docker >/dev/null 2>&1; then
  ( cd "$DIR/../deploy/nmos" && sudo docker compose up -d ) || echo "  ! docker compose up failed"
else
  echo "  ! docker not found -- run ./install.sh first"
fi

echo "[2/4] Panel (:$PANEL_PORT)..."
setsid bash "$DIR/monitor-run.sh" >/tmp/atoll-panel.log 2>&1 </dev/null &
sleep 3
curl -s -o /dev/null -w "  panel http %{http_code}\n" --max-time 5 "http://localhost:$PANEL_PORT/" || true

echo "[3/4] Media pipeline (senders + monitor multiview)..."
bash "$DIR/launch-media.sh"

echo "[4/4] Up.  Panel: http://localhost:$PANEL_PORT   |   iPad: http://$PC_WIFI_IP:$PANEL_PORT"
echo "      If the island NIC has no static IP yet:  sudo ip addr add $ISLAND_PC_IP/24 dev <nic>"
