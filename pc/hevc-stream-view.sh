#!/usr/bin/env bash
# 4K HEVC NVDEC viewer: RTP island multicast -> nvh265dec (GPU) -> GL downscale -> GPU window.
# Run in a LOCAL WSL terminal (needs WSLg display):  bash pc/hevc-stream-view.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"

echo "HEVC NVDEC viewer <- udp://$HEVC_ADDR:$HEVC_PORT on $HEVC_IFACE  (GPU window; close window or Ctrl+C to stop)"
# Center the window on screen 1 (Wayland can't self-position) -- reuse the JPEG-XS helper.
if command -v powershell.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
  CENTER_PS1="$(wslpath -w "$DIR/jxs-center-window.ps1" 2>/dev/null)"
  [ -n "$CENTER_PS1" ] && powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$CENTER_PS1" >/dev/null 2>&1 &
fi

exec gst-launch-1.0 -q \
  udpsrc address="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true buffer-size=8388608 caps="${HEVC_CAPS}" \
  ! rtpjitterbuffer latency=200 ! rtph265depay ! h265parse ! nvh265dec \
  ! cudadownload ! videoconvert ! glupload ! glcolorscale ! "video/x-raw(memory:GLMemory),width=${HEVC_DISPLAY_W},height=${HEVC_DISPLAY_H}" \
  ! glimagesink sync=false
