#!/usr/bin/env bash
# Island media sender (playlist): decode ANY file(s) and stream them as an island source.
# Aspect ratio is preserved (pillar/letter-boxed to the target geometry, never stretched);
# clips are cycled forever, so this doubles as a simple playlist source.
#
#   --hevc (default): GStreamer decodebin -> HEVC (NVENC) + MP3 -> MPEG-TS -> 239.10.10.65:5010
#   --jxs           : ffmpeg decode -> JPEG-XS (4:2:2) -> MPEG-TS -> 239.10.10.22:5008
#
# Usage:  bash pc/media-send.sh [--hevc|--jxs] <file> [file ...]
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET=hevc
case "${1:-}" in --jxs) TARGET=jxs; shift;; --hevc) TARGET=hevc; shift;; esac
source "$DIR/hevc-stream-env.sh"      # HEVC_ADDR / HEVC_PORT / HEVC_IFACE / HEVC_TTL
source "$DIR/jxs-stream-env.sh"       # JXS_ADDR / JXS_PORT / JXS_W/H/FPS/PIXFMT/BPP / JXS_LOCALADDR / JXS_TTL
[ "$#" -ge 1 ] || { echo "usage: $(basename "$0") [--hevc|--jxs] <file> [file ...]" >&2; exit 1; }
echo "media-send[$TARGET]: $# clip(s)  (Ctrl+C to stop)"

# HEVC: decodebin -> conform to 720p30 NV12 (videoscale add-borders preserves aspect) ->
# CUDA -> NVENC HEVC ; audio -> MP3. config-interval=-1 re-sends headers for mid-join.
send_hevc() {
  gst-launch-1.0 -e \
    filesrc location="$1" ! decodebin name=dec \
    dec. ! videorate ! videoscale add-borders=true ! videoconvert \
      ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1,pixel-aspect-ratio=1/1 ! cudaupload \
      ! nvh265enc rc-mode=cbr bitrate=12000 preset=p4 tune=low-latency gop-size=30 aud=true \
      ! h265parse config-interval=-1 ! queue ! mux. \
    dec. ! audioconvert ! audioresample ! lamemp3enc target=bitrate bitrate=192 ! mpegaudioparse ! queue ! mux. \
    mpegtsmux name=mux alignment=7 ! queue \
      ! udpsink host="$HEVC_ADDR" port="$HEVC_PORT" multicast-iface="$HEVC_IFACE" auto-multicast=true ttl="$HEVC_TTL" buffer-size=8388608 sync=true
}

# JPEG-XS: conform to the demo geometry, preserving aspect (decrease-then-pad pillarboxes
# 4:3 SD into 16:9). Matches the fixed-caps JXS viewer (1920x1080 @ 59.94 yuv422p).
send_jxs() {
  ffmpeg -hide_banner -loglevel warning -re -i "$1" \
    -map 0:v:0 -map 0:a:0? \
    -filter:v "scale=${JXS_W}:${JXS_H}:force_original_aspect_ratio=decrease,pad=${JXS_W}:${JXS_H}:(ow-iw)/2:(oh-ih)/2,fps=${JXS_FPS},format=${JXS_PIXFMT}" \
    -c:v jpegxs -bpp "${JXS_BPP}" -c:a aac -b:a 160k -ac 2 \
    -f mpegts "udp://${JXS_ADDR}:${JXS_PORT}?localaddr=${JXS_LOCALADDR}&ttl=${JXS_TTL}&pkt_size=1316&buffer_size=8388608"
}

while true; do
  for SRC in "$@"; do
    [ -f "$SRC" ] || { echo "skip (missing): $SRC"; continue; }
    echo ">> $(basename "$SRC")"
    if [ "$TARGET" = jxs ]; then send_jxs "$SRC" || echo "(clip failed: $SRC)"
    else send_hevc "$SRC" || echo "(clip failed: $SRC)"; fi
    sleep 0.3
  done
done
