#!/usr/bin/env bash
# 4K HEVC NVDEC viewer (fullscreen): RTP island multicast -> nvh265dec (NVDEC) ->
# CPU-downscale to 1080p -> waylandsink fullscreen (compositor upscales to the screen).
# Run in a LOCAL WSL terminal (needs WSLg display):  bash pc/hevc-stream-view.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"

echo "HEVC NVDEC viewer (fullscreen) <- udp://$HEVC_ADDR:$HEVC_PORT on $HEVC_IFACE  (Ctrl+C to stop)"

# Decode 4K on NVDEC, downscale to 1080p on CPU (a 4K frame through Mesa-D3D12 GL is
# only ~3 fps), then waylandsink fullscreen lets the WSLg compositor upscale to the
# screen on the GPU (cheap) -- smooth, no window-centering needed.
exec gst-launch-1.0 -q \
  udpsrc address="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true buffer-size=8388608 caps="${HEVC_CAPS}" \
  ! rtpjitterbuffer latency=200 ! rtph265depay ! h265parse ! nvh265dec \
  ! cudadownload ! videoconvert ! videoscale ! "video/x-raw,width=${HEVC_DISPLAY_W},height=${HEVC_DISPLAY_H}" \
  ! videoconvert ! waylandsink fullscreen=true sync=false
