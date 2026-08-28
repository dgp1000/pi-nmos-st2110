#!/usr/bin/env bash
# Atoll JPEG 2000 source: encode a source to J2K and multicast it as a genuine RTP flow
# (RFC 5371, rtpj2kpay) on the island. Unlike JPEG XS, J2K has a native gst RTP payloader AND
# a bitrate low enough to cross the WSL bridge, so this is a real receivable network tile.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, J2K_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
echo "j2k-send: JPEG 2000 (J2K/RTP RFC 5371) -> $J2K_GRP:$J2K_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q videotestsrc pattern=smpte is-live=true \
    ! video/x-raw,width=1280,height=720,framerate=30/1 \
    ! textoverlay text="JPEG 2000 - J2K over RTP (RFC 5371, OpenJPEG)" valignment=top halignment=center font-desc="Sans Bold 24" shaded-background=true \
    ! clockoverlay valignment=bottom halignment=right time-format="%H:%M:%S" font-desc="Sans Bold 18" shaded-background=true \
    ! videoconvert ! video/x-raw,format=I420 \
    ! avenc_jpeg2000 ! jpeg2000parse ! rtpj2kpay \
    ! udpsink host=$J2K_GRP port=$J2K_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(j2k sender dropped -- restart in 2s)"; sleep 2
done
