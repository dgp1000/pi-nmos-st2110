#!/usr/bin/env bash
# Atoll music channel with self-healing fallback: bridge the Mac "Now Playing" stream onto
# 5012 when the Mac is reachable, else feed the "connecting…" placeholder card so the
# multiview compositor never sees an empty tile. Re-evaluates every cycle, so it switches
# automatically as the Mac comes and goes. One sender on 5012 at a time (bridge OR card).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, MUSIC_*, MAC_MUSIC_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
MACBASE="http://$MAC_MUSIC_HOST:$MAC_MUSIC_PORT"

mac_up() { curl -s --max-time 4 -o /dev/null "$MACBASE/state"; }

# Live bridge: Mac H.264 TS -> NVDEC -> 720p30 -> NVENC HEVC -> video-only TS on MUSIC_GRP,
# plus AAC -> ST 2110-30 L24 (rtpL24pay) on MUSIC_AUDIO_GRP (audio-follows-source).
# Returns (non-zero) when the stream drops or the Mac goes away, so the loop re-checks.
run_bridge() {
  gst-launch-1.0 -q \
    souphttpsrc location="$MAC_MUSIC_TS" is-live=true ! tsdemux name=d \
    d. ! h264parse ! nvh264dec ! cudadownload \
      ! videorate ! videoscale ! videoconvert ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1 \
      ! cudaupload ! nvh265enc rc-mode=cbr bitrate=6000 preset=p4 tune=low-latency gop-size=30 aud=true \
      ! h265parse config-interval=-1 ! queue ! mux. \
    d. ! queue ! aacparse ! avdec_aac ! audioconvert ! audioresample ! audio/x-raw,format=S24BE,rate=48000,channels=2 \
      ! queue ! rtpL24pay pt=96 min-ptime=1000000 max-ptime=1000000 \
      ! udpsink host=$MUSIC_AUDIO_GRP port=$MUSIC_AUDIO_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL \
    mpegtsmux name=mux alignment=7 ! queue \
      ! udpsink host=$MUSIC_GRP port=$MUSIC_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
}

# Placeholder card for $1 seconds, then return so the loop can re-check the Mac.
run_placeholder() {
  timeout "$1" gst-launch-1.0 -q videotestsrc pattern=blue is-live=true \
    ! video/x-raw,width=1280,height=720,framerate=30/1 \
    ! textoverlay text="MUSIC — connecting…" valignment=center halignment=center font-desc="Sans Bold 40" shaded-background=true \
    ! videoconvert ! video/x-raw,format=NV12 ! cudaupload \
    ! nvh265enc rc-mode=cbr bitrate=4000 gop-size=30 aud=true ! h265parse config-interval=-1 ! queue \
    ! mpegtsmux alignment=7 ! queue \
    ! udpsink host=$MUSIC_GRP port=$MUSIC_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
}

echo "music-channel -> $MUSIC_GRP:$MUSIC_PORT  (bridge when Mac up, placeholder when down)"
while true; do
  if mac_up; then
    echo "$(date +%T) Mac reachable -> live bridge"
    run_bridge || true
  else
    echo "$(date +%T) Mac unreachable -> placeholder card (15s, then re-check)"
    run_placeholder 15 || true
  fi
  sleep 1
done
