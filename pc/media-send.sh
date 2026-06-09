#!/usr/bin/env bash
# Island media sender (playlist): decode ANY file(s) -> HEVC (NVENC) + MP3 -> MPEG-TS ->
# an island multicast group. Aspect preserved (pillar/letter-boxed, never stretched);
# clips cycle forever, so this doubles as a simple playlist source. HEVC is used for both
# targets so the GStreamer viewers / multiview can decode them natively.
#
#   --hevc (default): -> 239.10.10.65:5010   ("PC HEVC 4K" source)
#   --jxs           : -> 239.10.10.22:5008   ("Home videos" / receiver m0 source)
#
# Usage:  bash pc/media-send.sh [--hevc|--jxs] <file> [file ...]
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"      # HEVC_ADDR / HEVC_PORT / HEVC_IFACE / HEVC_TTL
source "$DIR/jxs-stream-env.sh"       # JXS_ADDR / JXS_PORT
GRP="$HEVC_ADDR"; PORT="$HEVC_PORT"; TAG=hevc
while [ -n "${1:-}" ]; do
  case "$1" in
    --jxs)  GRP="$JXS_ADDR"; PORT="$JXS_PORT"; TAG=jxs; shift;;
    --hevc) shift;;
    *) break;;
  esac
done
IFACE="${HEVC_IFACE:-eth0}"
[ "$#" -ge 1 ] || { echo "usage: $(basename "$0") [--hevc|--jxs] <file|dir> [file ...]" >&2; exit 1; }
# A single directory arg -> cycle every video file in it (sidesteps spaces/quotes/apostrophes
# in filenames, and auto-includes anything dropped in later).
if [ "$#" -eq 1 ] && [ -d "$1" ]; then
  shopt -s nullglob
  D="$1"; set -- "$D"/*.mpg "$D"/*.mp4 "$D"/*.mov "$D"/*.mkv "$D"/*.avi "$D"/*.m4v
  [ "$#" -ge 1 ] || { echo "no video files in: $D" >&2; exit 1; }
fi
echo "media-send[$TAG]: $# clip(s) -> $GRP:$PORT on $IFACE  (Ctrl+C to stop)"

# decodebin -> conform to 720p30 NV12 (videoscale add-borders preserves aspect) -> CUDA ->
# NVENC HEVC; audio -> MP3. config-interval=-1 re-sends headers so mid-stream joiners sync.
send() {
  # decodebin3 -- the legacy 'decodebin' fails to autoplug H.264 decoders on this GStreamer
  # 1.28 build. decodebin3 also selects a single stream per type, so files with multiple audio
  # tracks (e.g. bbb-4k's mp3+ac3) don't leave an unlinked pad that EOS's the pipeline.
  gst-launch-1.0 -e \
    filesrc location="$1" ! decodebin3 name=dec \
    dec. ! videorate ! videoscale add-borders=true ! videoconvert \
      ! video/x-raw,format=NV12,width=1280,height=720,framerate=30/1,pixel-aspect-ratio=1/1 ! cudaupload \
      ! nvh265enc rc-mode=cbr bitrate=12000 preset=p4 tune=low-latency gop-size=30 aud=true \
      ! h265parse config-interval=-1 ! queue ! mux. \
    dec. ! audioconvert ! audioresample ! lamemp3enc target=bitrate bitrate=192 ! mpegaudioparse ! queue ! mux. \
    mpegtsmux name=mux alignment=7 ! queue \
      ! udpsink host="$GRP" port="$PORT" multicast-iface="$IFACE" auto-multicast=true ttl="${HEVC_TTL:-1}" buffer-size=8388608 sync=true
}
while true; do
  for SRC in "$@"; do
    [ -f "$SRC" ] || { echo "skip (missing): $SRC"; continue; }
    echo ">> $(basename "$SRC")"
    send "$SRC" || echo "(clip failed: $SRC)"
    sleep 0.3
  done
done
