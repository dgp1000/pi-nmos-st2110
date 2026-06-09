#!/usr/bin/env bash
# Atoll music channel (Option B -- the chosen path): bridge the Mac's "Now Playing" stream
# onto the broadcast island, fully headless (no browser anywhere).
#
#   Mac (WiFi)  http://192.168.6.159:8008/nowplaying.ts  (H.264 720p MPEG-TS, looping)
#        |  WSL pulls over WiFi (mirrored networking)
#        v  re-encode to 720p HEVC (NVENC) so the multiview decodes it like the other tiles
#   island multicast 239.10.10.30:5012  ->  output-render.sh music tile
#
# Replaces pc/music-placeholder.sh on 5012 -- stop that first (one sender per group).
# Loops/retries so it survives the endpoint being briefly unavailable.
#
# Run:  bash pc/music-send.sh            (default Mac URL)
#       bash pc/music-send.sh <ts-url>   (override)
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hevc-stream-env.sh"   # HEVC_IFACE
IFACE="${HEVC_IFACE:-eth0}"
SRC="${1:-http://192.168.6.159:8008/nowplaying.ts}"
echo "music-send: $SRC -> 239.10.10.30:5012 on $IFACE  (Ctrl+C to stop)"
while true; do
  # pull H.264 TS -> NVDEC -> conform to 720p30 -> NVENC HEVC -> MPEG-TS -> island.
  # tsdemux links the video pad to h264parse; any audio pad stays unlinked (harmless).
  # config-interval=-1 re-sends VPS/SPS/PPS each IDR for mid-stream joiners.
  gst-launch-1.0 -q \
    souphttpsrc location="$SRC" is-live=true ! tsdemux ! h264parse ! nvh264dec ! cudadownload \
      ! videorate ! videoscale ! videoconvert ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1 \
      ! cudaupload ! nvh265enc rc-mode=cbr bitrate=6000 preset=p4 tune=low-latency gop-size=30 aud=true \
      ! h265parse config-interval=-1 ! queue ! mpegtsmux ! queue \
      ! udpsink host=239.10.10.30 port=5012 multicast-iface="$IFACE" auto-multicast=true ttl=1
  echo "(stream unavailable/dropped -- retry in 3s)"; sleep 3
done
