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
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/atoll.conf"   # ISLAND_IFACE, MUSIC_*, MAC_MUSIC_TS
IFACE="${ISLAND_IFACE:-eth0}"
SRC="${1:-$MAC_MUSIC_TS}"
echo "music-send: $SRC -> $MUSIC_GRP:$MUSIC_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  # pull the H.264+AAC TS -> NVDEC video, conform to 720p30, NVENC HEVC; AAC audio -> MP3.
  # Both are re-muxed into one MPEG-TS on the island (5012) so the multiview tile decodes the
  # video and the audio-follower plays the MP3 (same model as the other tiles). h264parse and
  # aacparse pick the right tsdemux pads by caps. config-interval=-1 re-sends headers for joiners.
  gst-launch-1.0 -q \
    souphttpsrc location="$SRC" is-live=true ! tsdemux name=d \
    d. ! h264parse ! nvh264dec ! cudadownload \
      ! videorate ! videoscale ! videoconvert ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1 \
      ! cudaupload ! nvh265enc rc-mode=cbr bitrate=6000 preset=p4 tune=low-latency gop-size=30 aud=true \
      ! h265parse config-interval=-1 ! queue ! mux. \
    d. ! aacparse ! avdec_aac ! audioconvert ! audioresample ! lamemp3enc target=bitrate bitrate=192 ! mpegaudioparse ! queue ! mux. \
    mpegtsmux name=mux alignment=7 ! queue \
      ! udpsink host=$MUSIC_GRP port=$MUSIC_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(stream unavailable/dropped -- retry in 3s)"; sleep 3
done
