#!/usr/bin/env bash
# 4K HEVC NVENC sender: synthetic 4K source -> nvh265enc (GPU) -> RTP -> island multicast.
# Run in WSL:  bash pc/hevc-stream-send.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"

echo "HEVC NVENC sender -> udp://$HEVC_ADDR:$HEVC_PORT via $HEVC_IFACE  (${HEVC_W}x${HEVC_H} @ ${HEVC_FPS}, ${HEVC_BITRATE} kbit CBR)"
echo "Ctrl+C to stop."
exec gst-launch-1.0 -q \
  videotestsrc is-live=true ! "video/x-raw,width=${HEVC_W},height=${HEVC_H},framerate=${HEVC_FPS}" \
  ! videoconvert \
  ! nvh265enc rc-mode=cbr bitrate="${HEVC_BITRATE}" preset=p4 tune=low-latency gop-size="${HEVC_GOP}" aud=true \
  ! h265parse ! rtph265pay config-interval=-1 pt=96 \
  ! udpsink host="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true ttl="${HEVC_TTL}" buffer-size=8388608
