#!/usr/bin/env bash
# JPEG-XS streaming SENDER (synthetic source): generates a 1080p59.94 test
# pattern, encodes it to JPEG-XS, muxes to MPEG-TS, and streams over UDP.
# Run in a WSL terminal:  bash pc/jxs-stream-send.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/jxs-stream-env.sh"

echo "JPEG-XS sender -> udp://$JXS_ADDR:$JXS_PORT via $JXS_LOCALADDR  (${JXS_W}x${JXS_H} @ ${JXS_FPS}, bpp ${JXS_BPP})"
echo "Ctrl+C to stop."
exec ffmpeg -hide_banner -loglevel warning \
  -f lavfi -i "testsrc=size=${JXS_W}x${JXS_H}:rate=${JXS_FPS}" \
  -vf "format=${JXS_PIXFMT}" \
  -c:v jpegxs -bpp "${JXS_BPP}" \
  -f mpegts "udp://${JXS_ADDR}:${JXS_PORT}?localaddr=${JXS_LOCALADDR}&ttl=${JXS_TTL}&pkt_size=1316&buffer_size=8388608"
