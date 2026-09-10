#!/usr/bin/env bash
# Atoll H.265/HEVC over RTP (RFC 7798, rtph265pay) on the island -- a dedicated raw H.265 elementary
# stream (unlike the Live-TV HEVC-in-MPEG-TS channel), so BCP-006-02 has a real video/H265 essence to
# advertise. Synthetic 720p test pattern; NVENC HEVC. Its own group (H265_GRP), distinct from 5010.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"
IFACE="${ISLAND_IFACE:-eth0}"
BITRATE="${H265_BITRATE:-4000}"
echo "h265-send: H.265 over RTP (RFC 7798, NVENC) -> $H265_GRP:$H265_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q videotestsrc pattern=smpte75 is-live=true \
    ! video/x-raw,width=1280,height=720,framerate=30/1 \
    ! cudaupload ! nvh265enc rc-mode=cbr bitrate=$BITRATE preset=p4 tune=low-latency gop-size=30 aud=true \
    ! h265parse ! rtph265pay pt=96 config-interval=-1 \
    ! udpsink host=$H265_GRP port=$H265_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(h265 sender dropped -- restart in 2s)"; sleep 2
done
