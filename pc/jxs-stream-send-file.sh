#!/usr/bin/env bash
# JPEG-XS streaming SENDER from a video FILE (vs the synthetic-testsrc sender).
# Reads any video, conforms it to the demo geometry/frame rate, encodes JPEG-XS,
# and multicasts it on the island. Loops forever by default.
# Usage:  bash pc/jxs-stream-send-file.sh <video-file> [--once]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/jxs-stream-env.sh"

SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "usage: $(basename "$0") <video-file> [--once]   (file not found: '$SRC')" >&2
  exit 1
fi

# Loop the file forever unless --once is given.
LOOP=(-stream_loop -1)
[ "${2:-}" = "--once" ] && LOOP=()

echo "JPEG-XS file sender: $SRC"
echo "  -> udp://$JXS_ADDR:$JXS_PORT via $JXS_LOCALADDR  (${JXS_W}x${JXS_H} @ ${JXS_FPS}, bpp ${JXS_BPP})"
echo "  Ctrl+C to stop."

# -re paces input at real time; scale/fps conform any source to the demo geometry
# (upscales 720p, conforms frame rate to 59.94 so the fixed-caps viewer matches).
exec ffmpeg -hide_banner -loglevel warning -re "${LOOP[@]}" -i "$SRC" \
  -vf "scale=${JXS_W}:${JXS_H},fps=${JXS_FPS},format=${JXS_PIXFMT}" \
  -c:v jpegxs -bpp "${JXS_BPP}" -an \
  -f mpegts "udp://${JXS_ADDR}:${JXS_PORT}?localaddr=${JXS_LOCALADDR}&ttl=${JXS_TTL}&pkt_size=1316&buffer_size=8388608"
