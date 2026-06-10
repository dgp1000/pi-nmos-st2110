#!/usr/bin/env bash
# Relaunch ALL Pi-side generators after a PC reboot.
# Run on the Pi as root:   sudo bash ~/launch-all.sh
# Uses setsid so everything survives SSH disconnects.
source "$(cd "$(dirname "$0")" && pwd)/atoll-pi.conf"   # PI_AUDIO_*/PI_RAW_*, ISLAND_IFACE, USERHOME, CLOCK_PORT
L=/tmp

echo "stopping any existing generators..."
pkill -x ptp4l 2>/dev/null; pkill -x gst-launch-1.0 2>/dev/null; pkill -f master-clock-web.py 2>/dev/null
sleep 1

echo "[1] PTP grandmaster ($ISLAND_IFACE)..."
setsid ptp4l -i "$ISLAND_IFACE" -S >"$L/ptp4l.log" 2>&1 </dev/null &

echo "[2] ST 2110-30 audio sender -> $PI_AUDIO_GRP:$PI_AUDIO_PORT ..."
setsid gst-launch-1.0 audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample \
  ! audio/x-raw,format=S24BE,rate=48000,channels=2 \
  ! rtpL24pay pt=96 min-ptime=1000000 max-ptime=1000000 \
  ! udpsink host="$PI_AUDIO_GRP" port="$PI_AUDIO_PORT" auto-multicast=true multicast-iface="$ISLAND_IFACE" ttl-mc="$MCAST_TTL" \
  >"$L/audio.log" 2>&1 </dev/null &

echo "[3] ST 2110-20 video sender 59.94 -> $PI_RAW_GRP:$PI_RAW_PORT ..."
setsid gst-launch-1.0 videotestsrc is-live=true \
  ! video/x-raw,format=UYVY,width=320,height=240,framerate=60000/1001 \
  ! rtpvrawpay ! udpsink host="$PI_RAW_GRP" port="$PI_RAW_PORT" auto-multicast=true multicast-iface="$ISLAND_IFACE" ttl-mc="$MCAST_TTL" \
  >"$L/video.log" 2>&1 </dev/null &

echo "[4] PTP web clock -> http://pi5-nmos.local:$CLOCK_PORT ..."
setsid python3 "$USERHOME/master-clock-web.py" >"$L/webclock.log" 2>&1 </dev/null &

sleep 2
echo; echo "=== running now ==="
pgrep -a ptp4l; pgrep -a gst-launch-1.0; pgrep -fa master-clock-web.py | grep -v grep
echo
echo "Optional terminal studio clock (interactive): sudo python3 ~/master-clock.py"
