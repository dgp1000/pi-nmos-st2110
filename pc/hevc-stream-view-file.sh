#!/usr/bin/env bash
# HEVC file viewer (A/V): receive the MPEG-TS island stream, GPU-decode HEVC (NVDEC),
# GL-downscale to a window, and play the MP3 audio in lip-sync with it.
# Run in a LOCAL WSL terminal (needs WSLg display + audio):  bash pc/hevc-stream-view-file.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"
export PULSE_SERVER="${PULSE_SERVER:-unix:/mnt/wslg/PulseServer}"   # WSLg audio

echo "HEVC file viewer (A/V) <- mpegts udp://$HEVC_ADDR:$HEVC_PORT on $HEVC_IFACE  (close window or Ctrl+C to stop)"
# Center the window on screen 1 (Wayland can't self-position) -- reuse the JPEG-XS helper.
if command -v powershell.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
  CENTER_PS1="$(wslpath -w "$DIR/jxs-center-window.ps1" 2>/dev/null)"
  [ -n "$CENTER_PS1" ] && powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$CENTER_PS1" >/dev/null 2>&1 &
fi

# tsdemux pads are routed by caps: h265parse (video) and mpegaudioparse (audio) come
# first so each elementary stream links to the right branch; queues decouple them.
# Both sinks sync=true -> lip-synced A/V off the shared MPEG-TS timeline.
exec gst-launch-1.0 -q \
  udpsrc address="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true buffer-size=8388608 \
    ! tsdemux name=d \
  d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert ! videoscale \
    ! "video/x-raw,width=${HEVC_DISPLAY_W},height=${HEVC_DISPLAY_H}" ! glimagesink sync=true \
  d. ! mpegaudioparse ! queue ! mpg123audiodec ! audioconvert ! audioresample ! autoaudiosink sync=true
