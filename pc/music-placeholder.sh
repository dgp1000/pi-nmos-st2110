#!/usr/bin/env bash
# Placeholder feed for the Atoll "music" tile (239.10.10.30:5012) until the Mac "Now Playing"
# stream is bridged in. The multiview's compositor STALLS if a tile's pad never gets caps, so
# this keeps 5012 alive with a "connecting" card. The real music sender (Mac bridge) replaces
# this on 5012 (stop this first -- one sender per group). Loops so it self-restarts.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hevc-stream-env.sh"   # HEVC_IFACE
IFACE="${HEVC_IFACE:-eth0}"
echo "music-placeholder -> 239.10.10.30:5012 on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q videotestsrc pattern=blue is-live=true \
    ! video/x-raw,width=1280,height=720,framerate=30/1 \
    ! textoverlay text="MUSIC — connecting…" valignment=center halignment=center font-desc="Sans Bold 40" shaded-background=true \
    ! videoconvert ! video/x-raw,format=NV12 ! cudaupload \
    ! nvh265enc rc-mode=cbr bitrate=4000 gop-size=30 aud=true ! h265parse config-interval=-1 ! queue \
    ! mpegtsmux ! queue \
    ! udpsink host=239.10.10.30 port=5012 multicast-iface="$IFACE" auto-multicast=true ttl=1
  echo "(placeholder dropped -- restarting)"; sleep 1
done
