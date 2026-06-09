#!/usr/bin/env bash
# HEVC file sender: GPU-decode a video file (NVDEC) and re-encode to H.265 (NVENC),
# then RTP-multicast on the island. Both GPU video engines run at once.
# Usage:  bash pc/hevc-stream-send-file.sh <video-file>
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"

SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "usage: $(basename "$0") <video-file>   (file not found: '$SRC')" >&2
  exit 1
fi

echo "HEVC file sender: $SRC"
echo "  -> udp://$HEVC_ADDR:$HEVC_PORT via $HEVC_IFACE  (NVDEC decode + NVENC HEVC re-encode)"
echo "  Ctrl+C to stop."

# parsebin demuxes/parses the container; nvh264dec decodes on the GPU (NVDEC);
# nvh265enc re-encodes on the GPU (NVENC) -- frames stay in CUDA memory. udpsink
# sync=true paces the non-live file at real time.
exec gst-launch-1.0 -q \
  filesrc location="$SRC" ! parsebin ! nvh264dec \
  ! queue ! nvh265enc rc-mode=cbr bitrate="${HEVC_BITRATE}" preset=p4 tune=low-latency gop-size="${HEVC_GOP}" aud=true \
  ! h265parse ! rtph265pay config-interval=-1 pt=96 \
  ! udpsink host="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true ttl="${HEVC_TTL}" buffer-size=8388608 sync=true
