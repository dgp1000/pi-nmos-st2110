#!/usr/bin/env bash
# HEVC file viewer (A/V, fullscreen): receive the MPEG-TS island stream, GPU-decode
# HEVC (NVDEC), CPU-downscale to 1080p, and display fullscreen with lip-synced MP3 audio.
# Run in a LOCAL WSL terminal (needs WSLg display + audio):  bash pc/hevc-stream-view-file.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"
export PULSE_SERVER="${PULSE_SERVER:-unix:/mnt/wslg/PulseServer}"   # WSLg audio

echo "HEVC file viewer (A/V, fullscreen) <- mpegts udp://$HEVC_ADDR:$HEVC_PORT on $HEVC_IFACE  (Ctrl+C to stop)"

# Decode 4K on NVDEC (the workout), downscale to 1080p on CPU (smooth -- a 4K frame
# through Mesa-D3D12 GL is only ~3 fps), then waylandsink fullscreen lets the WSLg
# compositor upscale to the screen on the GPU (cheap). No window-centering needed in
# fullscreen. tsdemux pads route by caps (h265parse / mpegaudioparse); both sinks
# sync=true off the shared MPEG-TS timeline -> lip-synced A/V.
exec gst-launch-1.0 -q \
  udpsrc address="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true buffer-size=8388608 \
    ! tsdemux name=d \
  d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert ! videoscale \
    ! "video/x-raw,width=${HEVC_DISPLAY_W},height=${HEVC_DISPLAY_H}" ! videoconvert ! waylandsink fullscreen=true sync=true \
  d. ! mpegaudioparse ! queue ! mpg123audiodec ! audioconvert ! audioresample ! autoaudiosink sync=true
