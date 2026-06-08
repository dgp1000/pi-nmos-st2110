#!/usr/bin/env bash
# Relaunch ALL Pi-side generators after a PC reboot.
# Run on the Pi as root:   sudo bash ~/launch-all.sh
# Uses setsid so everything survives SSH disconnects.
USERHOME=/home/dgperkins
L=/tmp

echo "stopping any existing generators..."
pkill -x ptp4l 2>/dev/null; pkill -x gst-launch-1.0 2>/dev/null; pkill -f master-clock-web.py 2>/dev/null
sleep 1

echo "[1] PTP grandmaster (eth0)..."
setsid ptp4l -i eth0 -S >"$L/ptp4l.log" 2>&1 </dev/null &

echo "[2] ST 2110-30 audio sender -> 239.10.10.10:5004 ..."
setsid gst-launch-1.0 audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample \
  ! audio/x-raw,format=S24BE,rate=48000,channels=2 \
  ! rtpL24pay pt=96 min-ptime=1000000 max-ptime=1000000 \
  ! udpsink host=239.10.10.10 port=5004 auto-multicast=true multicast-iface=eth0 ttl-mc=1 \
  >"$L/audio.log" 2>&1 </dev/null &

echo "[3] ST 2110-20 video sender 59.94 -> 239.10.10.20:5005 ..."
setsid gst-launch-1.0 videotestsrc is-live=true \
  ! video/x-raw,format=UYVY,width=320,height=240,framerate=60000/1001 \
  ! rtpvrawpay ! udpsink host=239.10.10.20 port=5005 auto-multicast=true multicast-iface=eth0 ttl-mc=1 \
  >"$L/video.log" 2>&1 </dev/null &

echo "[4] PTP web clock -> http://pi5-nmos.local:8000 ..."
setsid python3 "$USERHOME/master-clock-web.py" >"$L/webclock.log" 2>&1 </dev/null &

sleep 2
echo; echo "=== running now ==="
pgrep -a ptp4l; pgrep -a gst-launch-1.0; pgrep -fa master-clock-web.py | grep -v grep
echo
echo "Optional terminal studio clock (interactive): sudo python3 ~/master-clock.py"
