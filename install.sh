#!/usr/bin/env bash
# ===========================================================================
#  Atoll installer — set up the PC/host side on Debian/Ubuntu (WSL2 or native).
#  Installs the GStreamer/NVENC + Docker + Python deps, brings up the NMOS
#  stack, and points you at the config. Idempotent; safe to re-run.
#
#    bash install.sh            # full install
#    bash install.sh --check    # checks only (no apt, no docker), good for diagnosis
#
#  Requires: an NVIDIA GPU (NVENC/NVDEC), a wired NIC for the ST 2110 island,
#  and an IGMP-snooping switch. The Pi side is set up separately (see pi/).
# ===========================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1

c_hdr=$'\033[1;36m'; c_warn=$'\033[1;33m'; c_ok=$'\033[1;32m'; c_off=$'\033[0m'
say()  { printf '\n%s== %s%s\n' "$c_hdr" "$*" "$c_off"; }
ok()   { printf '   %s✓%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '   %s! %s%s\n' "$c_warn" "$*" "$c_off"; }

say "Atoll installer (Debian/Ubuntu; WSL2 or Linux-native)"

# --- 1. apt dependencies -----------------------------------------------------
if [ "$CHECK_ONLY" = 0 ]; then
  say "[1/6] Installing packages (GStreamer + NVENC plugins, ffmpeg, Docker, Python)…"
  sudo apt-get update -qq || warn "apt update failed"
  sudo apt-get install -y \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
    gstreamer1.0-pulseaudio gstreamer1.0-gl \
    ffmpeg python3 python3-pip curl jq iproute2 docker.io docker-compose-v2 \
    && ok "packages installed" \
    || warn "some packages failed (try 'docker-compose-plugin' if 'docker-compose-v2' is unavailable)"
else
  say "[1/6] (--check) skipping apt install"
fi

# --- 2. GPU / NVENC ----------------------------------------------------------
say "[2/6] NVIDIA GPU + GStreamer NVENC/NVDEC plugins…"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L 2>/dev/null | grep -q GPU; then
  nvidia-smi -L | sed 's/^/   /'
else
  warn "no nvidia-smi / GPU — Atoll's HEVC encode+decode REQUIRE an NVIDIA GPU"
fi
gst-inspect-1.0 >/dev/null 2>&1   # ensure the plugin registry is built (full scan, never partial)
miss=0
for el in nvh265enc nvh265dec nvh264dec cudaupload cudadownload compositor waylandsink mpegtsmux rtpL24depay; do
  if gst-inspect-1.0 "$el" >/dev/null 2>&1; then ok "$el"; else warn "missing gst element: $el"; miss=1; fi
done
[ "$miss" = 1 ] && warn "missing elements usually mean -plugins-bad/-libav not installed or no NVIDIA runtime"

# --- 3. NMOS stack (Docker) --------------------------------------------------
say "[3/6] NMOS stack (nmos-cpp registry :8080 + virtnode :8090 + AMWA testing :5000)…"
if [ "$CHECK_ONLY" = 0 ]; then
  sudo systemctl start docker 2>/dev/null || warn "could not start docker via systemctl (start dockerd / Docker Desktop yourself)"
  if sudo docker info >/dev/null 2>&1; then
    ( cd "$REPO/deploy/nmos" && sudo docker compose up -d ) && ok "NMOS stack up" || warn "docker compose up failed"
  else
    warn "Docker not reachable — skipping NMOS bring-up"
  fi
fi
for p in 8080 8090 5000; do
  if curl -s -o /dev/null --max-time 3 "http://localhost:$p/"; then ok "port $p responding"; else warn "port $p not responding yet (containers may still be starting)"; fi
done

# --- 4. config ---------------------------------------------------------------
say "[4/6] Config (pc/atoll.conf)…"
if [ -f "$REPO/pc/atoll.conf" ]; then
  ok "pc/atoll.conf present — edit it for your environment:"
  echo "       ATOLL_USER · ISLAND_PC_IP/ISLAND_PI_IP · the 6 multicast groups · media paths"
  echo "       (the Pi's pi/atoll-pi.conf groups MUST match)"
else
  warn "pc/atoll.conf missing (unexpected in a clone)"
fi

# --- 5. media ----------------------------------------------------------------
say "[5/6] Media files…"
# shellcheck disable=SC1091
source "$REPO/pc/atoll.conf" 2>/dev/null || true
[ -n "${BBB_FILE:-}" ] && { [ -f "$BBB_FILE" ] && ok "bbb demo: $BBB_FILE" || warn "bbb demo missing at $BBB_FILE — drop a Big Buck Bunny mp4 (CC-BY) there or repoint BBB_FILE"; }
[ -n "${REELS_LOOP:-}" ] && { [ -f "$REELS_LOOP" ] && ok "reels loop: $REELS_LOOP" || warn "reels loop missing — build with pc/build-reels-loop.ps1 (or concat your clips)"; }

# --- 6. network --------------------------------------------------------------
say "[6/6] Network (the ST 2110 island)…"
echo "   Give the wired NIC a static island IP (${ISLAND_PC_IP:-10.10.10.2}/24) on an IGMP-snooping switch:"
echo "     Linux:    sudo ip addr add ${ISLAND_PC_IP:-10.10.10.2}/24 dev <nic>"
echo "     Windows:  New-NetIPAddress -InterfaceAlias Ethernet -IPAddress ${ISLAND_PC_IP:-10.10.10.2} -PrefixLength 24   (admin)"

say "Done."
echo "   Next:"
echo "     1. Edit pc/atoll.conf  (and deploy pi/atoll-pi.conf + pi/launch-all.sh on the Pi)."
echo "     2. Start the rig:   bash pc/monitor-run.sh   then   bash pc/launch-media.sh"
echo "        (Windows/WSL one-shot:  powershell -ExecutionPolicy Bypass -File pc/restore.ps1)"
echo "     3. Open the panel:  http://<this-host>:${PANEL_PORT:-8096}"
