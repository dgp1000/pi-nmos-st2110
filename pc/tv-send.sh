#!/usr/bin/env bash
# Atoll live-TV bridge with channel switching. Tunes whatever channel is in the state file
# ($ATOLL_RUN/tv-channel, written by tv-web.py), re-tuning when it changes.
#   HDHR (WiFi) http://<hdhr>:5004/auto/v<ch> -> decodebin3 -> 720p HEVC (NVENC) + MP3
#   -> island HEVC_GRP:HEVC_PORT  ->  multiview "Live TV" tile.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"          # ISLAND_IFACE, HEVC_*, HDHR_HOST, TV_CHANNEL, ATOLL_RUN
IFACE="${ISLAND_IFACE:-eth0}"
STATE="$ATOLL_RUN/tv-channel"
mkdir -p "$ATOLL_RUN"
[ -f "$STATE" ] || echo "${TV_CHANNEL:-8.1}" > "$STATE"

start_gst() {   # $1 = channel; launches gst in background, sets $pid
  setsid gst-launch-1.0 -q souphttpsrc location="http://$HDHR_HOST:5004/auto/v$1" is-live=true ! decodebin3 name=dec \
    dec. ! queue ! videorate ! videoscale ! videoconvert ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1 \
      ! cudaupload ! nvh265enc rc-mode=cbr bitrate=6000 preset=p4 tune=low-latency gop-size=30 aud=true ! h265parse config-interval=-1 ! queue ! mux. \
    dec. ! queue ! audioconvert ! audioresample ! lamemp3enc target=bitrate bitrate=192 ! mpegaudioparse ! queue ! mux. \
    mpegtsmux name=mux alignment=7 ! queue ! udpsink host=$HEVC_GRP port=$HEVC_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL \
    >/dev/null 2>&1 &
  pid=$!
}

cur=""; pid=""
echo "tv-send: watching $STATE -> $HEVC_GRP:$HEVC_PORT on $IFACE"
while true; do
  want="$(tr -d ' \n\r' < "$STATE" 2>/dev/null)"; [ -z "$want" ] && want="${TV_CHANNEL:-8.1}"
  if [ "$want" != "$cur" ]; then
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    echo "$(date +%T) tuning channel $want"
    start_gst "$want"; cur="$want"
  elif [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
    echo "$(date +%T) gst for $cur exited -> retune in 3s"; sleep 3; cur=""
  fi
  sleep 1
done
