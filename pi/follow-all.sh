#!/usr/bin/env bash
# Atoll — 2nd-Pi PTP FOLLOWER. Locks this Pi's clock to the Pi 5 grandmaster over the
# island network and shows the offset converging. Run on the FOLLOWER Pi as root:
#     sudo bash ~/follow-all.sh
# Uses setsid so it survives SSH disconnects. Pi 4/3 lack hardware PTP timestamping,
# so we use software timestamping (-S) — looser than the grandmaster's HW path but it
# still converges to sub-100us. Domain 0 + E2E to match the grandmaster (ptp4l -i eth0 -S).
source "$(cd "$(dirname "$0")" && pwd)/atoll-pi.conf"   # ISLAND_IFACE, USERHOME, CLOCK_PORT
L=/tmp

echo "stopping any existing follower processes..."
pkill -x ptp4l 2>/dev/null; pkill -f follower-clock-web.py 2>/dev/null
sleep 1

echo "[1] PTP follower (slave-only) on $ISLAND_IFACE — locking to the grandmaster..."
# -s = slaveOnly (never wins BMCA, always a follower); -S = software timestamping;
# no -d => domain 0, matching the grandmaster. ptp4l -S disciplines the SYSTEM clock
# directly, so no phc2sys is needed on a software-timestamping follower.
setsid ptp4l -i "$ISLAND_IFACE" -s -S >"$L/ptp4l.log" 2>&1 </dev/null &

echo "[2] Follower web readout -> http://<this-pi>:$CLOCK_PORT ..."
setsid python3 "$USERHOME/follower-clock-web.py" >"$L/followerweb.log" 2>&1 </dev/null &

sleep 3
echo; echo "=== running now ==="
pgrep -a ptp4l; pgrep -fa follower-clock-web.py | grep -v grep
echo
echo "--- first PTP servo lines (state should climb s0 -> s1 -> s2 = locked) ---"
sleep 4
grep -E "ptp4l|offset|selected|assuming|new foreign|INITIALIZING|LISTENING|UNCALIBRATED|SLAVE" "$L/ptp4l.log" | tail -n 12
echo
echo "watch it converge:  tail -f $L/ptp4l.log"
echo "or open the web readout on any browser on the network."
