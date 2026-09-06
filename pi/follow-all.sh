#!/usr/bin/env bash
# Atoll — 2nd-Pi PTP FOLLOWER. Locks this Pi's clock to the Pi 5 grandmaster over the
# island network and shows the offset converging. Run on the FOLLOWER Pi as root:
#     sudo bash ~/follow-all.sh
# Uses setsid so it survives SSH disconnects. Pi 2/3/4 lack a PTP hardware clock, so we
# use software timestamping (see follower-ptp.cfg) — looser than the grandmaster's path
# but it still locks. The config uses HYBRID E2E (unicast Delay_Req) to work around
# IGMP-snooping-without-a-querier on the island switch, which otherwise makes the
# grandmaster ignore multicast delay requests. Domain 0 + E2E match the grandmaster
# (ptp4l -i eth0 -S). First lock STEPS this Pi's clock to adopt the master's time.
source "$(cd "$(dirname "$0")" && pwd)/atoll-pi.conf"   # ISLAND_IFACE, USERHOME, CLOCK_PORT
L=/tmp
CFG="$USERHOME/follower-ptp.cfg"

echo "stopping any existing follower processes..."
pkill -x ptp4l 2>/dev/null; pkill -f follower-clock-web.py 2>/dev/null
sleep 1

echo "[0] disabling system NTP so PTP is the sole clock authority..."
timedatectl set-ntp false 2>/dev/null
systemctl stop systemd-timesyncd 2>/dev/null

echo "[1] PTP follower (slave-only, hybrid E2E) on $ISLAND_IFACE — locking to the grandmaster..."
setsid ptp4l -f "$CFG" -i "$ISLAND_IFACE" -m >"$L/ptp4l.log" 2>&1 </dev/null &

echo "[2] Follower web readout -> http://<this-pi>:$CLOCK_PORT ..."
setsid python3 "$USERHOME/follower-clock-web.py" >"$L/followerweb.log" 2>&1 </dev/null &

sleep 8
echo; echo "=== running now ==="
pgrep -a ptp4l; pgrep -fa follower-clock-web.py | grep -v grep
echo
echo "--- PTP state (first lock steps the clock, then converges to SLAVE) ---"
pmc -u -b 0 "GET PORT_DATA_SET"  2>/dev/null | grep -E "portState"
pmc -u -b 0 "GET TIME_STATUS_NP" 2>/dev/null | grep -E "master_offset|gmPresent|gmIdentity"
echo
echo "watch it converge:  tail -f $L/ptp4l.log   (block-buffered; pmc and the web page are live)"
echo "web readout:        http://<this-pi>:$CLOCK_PORT"
