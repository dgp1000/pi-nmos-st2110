#!/usr/bin/env bash
# Atoll H.264 source: NVENC-encode a source and multicast it as a genuine RTP elementary stream
# (RFC 6184, rtph264pay) on the island. Unlike the HEVC channels -- which are MPEG-TS over bare UDP --
# this is video-over-RTP directly, the payload format used for IP contribution feeds, so it exercises
# a different receive path (rtpjitterbuffer -> rtph264depay) alongside the TS and 2110 flows.
#
# config-interval=-1 repeats SPS/PPS with every IDR: essential on multicast so a receiver joining
# mid-stream can start decoding at the next keyframe instead of waiting for a one-shot header.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, H264_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
BITRATE="${H264_BITRATE:-4000}" # kbit/s; a static pattern needs far less than the 6M HEVC channels
echo "h264-send: H.264 over RTP (RFC 6184, NVENC) -> $H264_GRP:$H264_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q videotestsrc pattern=smpte75 is-live=true \
    ! video/x-raw,width=1280,height=720,framerate=30/1 \
    ! clockoverlay valignment=bottom halignment=right time-format="%H:%M:%S" font-desc="Sans Bold 18" shaded-background=true \
    ! videoconvert ! video/x-raw,format=NV12 \
    ! cudaupload ! nvh264enc rc-mode=cbr bitrate=$BITRATE preset=p4 tune=low-latency gop-size=30 aud=true \
    ! h264parse config-interval=-1 \
    ! rtph264pay pt=96 config-interval=-1 \
    ! udpsink host=$H264_GRP port=$H264_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(h264 sender dropped -- restart in 2s)"; sleep 2
done
