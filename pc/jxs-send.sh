#!/usr/bin/env bash
# Atoll JPEG XS source: encode a live source to JPEG XS (image/x-jxsc, the ST 2110-22 codec via
# SVT-JPEG-XS) and multicast it on the island as MPEG-TS. The multiview/jxs-web decode it with
# svtjpegxsdec. This is JPEG-XS-over-TS (gst-native); true ST 2110-22 is the same codec over an
# RFC 9134 RTP payloader (future). Source is a moving test pattern; swap for any video source.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, JPEGXS_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
echo "jxs-send: JPEG XS -> $JPEGXS_GRP:$JPEGXS_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q videotestsrc pattern=ball is-live=true \
    ! video/x-raw,width=320,height=240,framerate=30/1 \
    ! textoverlay text="JPEG XS — ST 2110-22 codec (SVT-JPEG-XS)" valignment=top halignment=center font-desc="Sans Bold 26" shaded-background=true \
    ! clockoverlay valignment=bottom halignment=right time-format="%H:%M:%S" font-desc="Sans Bold 20" shaded-background=true \
    ! videoconvert ! video/x-raw,format=Y42B \
    ! svtjpegxsenc rate-control-mode=cbr-precinct bits-per-pixel=0.5 ! queue \
    ! mpegtsmux alignment=7 ! queue \
    ! udpsink host=$JPEGXS_GRP port=$JPEGXS_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(jxs sender dropped -- restart in 2s)"; sleep 2
done
