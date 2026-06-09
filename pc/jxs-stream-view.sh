#!/usr/bin/env bash
# JPEG-XS streaming RECEIVER + native GPU display. Receives JPEG-XS-in-MPEG-TS
# over UDP, decodes with FFmpeg, and shows it in a GPU-accelerated GStreamer
# window (Mesa D3D12). Reusable for any jpegxs/mpegts/udp source.
# Run in a LOCAL WSL terminal (needs WSLg display):  bash pc/jxs-stream-view.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/jxs-stream-env.sh"

echo "JPEG-XS viewer <- udp://$JXS_ADDR:$JXS_PORT  (GPU window; close window or Ctrl+C to stop)"
ffmpeg -hide_banner -loglevel warning \
    -fflags nobuffer -flags low_delay \
    -i "udp://${JXS_ADDR}:${JXS_PORT}?localaddr=${JXS_LOCALADDR}&overrun_nonfatal=1&fifo_size=5000000&buffer_size=67108864" \
    -f rawvideo -pix_fmt "${JXS_PIXFMT}" - \
  | gst-launch-1.0 -q \
      fdsrc ! rawvideoparse format="${JXS_GSTFMT}" width="${JXS_W}" height="${JXS_H}" framerate="${JXS_FPS}" \
      ! queue ! videoconvert ! glimagesink sync=false
