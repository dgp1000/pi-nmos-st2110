#!/usr/bin/env bash
# Atoll MPEG-TS over RTP source (SMPTE ST 2022-2 / RFC 2250): a full programme -- video AND audio
# multiplexed into one transport stream -- carried in RTP rather than dumped into bare UDP.
#
# This is the transport the rest of the rig's TS channels (Live TV, Home, Music) SHOULD arguably use:
# they send raw TS in UDP, which has no sequence numbers and no timestamps, so a receiver cannot tell
# a dropped datagram from a late one. Wrapping the same TS in RTP adds both, which is what makes loss
# detection and FEC (ST 2022-1) possible. MP2T is RTP payload type 33, a STATIC assignment from
# RFC 3551 -- unlike the dynamic 96/97 the other feeds negotiate.
#
# Encoded with x264enc on the CPU deliberately: this box is a Turing card whose NVENC session count is
# the scarce resource (Live TV, Home, Music and the H.264 feed already hold sessions), while 20 cores
# sit mostly idle. H.264-in-TS is also exactly what real contribution links carry.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, TSRTP_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
BITRATE="${TSRTP_BITRATE:-4000}"    # kbit/s (x264enc takes kbit/s)
echo "tsrtp-send: MPEG-TS over RTP (ST 2022-2, pt=33) -> $TSRTP_GRP:$TSRTP_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q \
    videotestsrc pattern=pinwheel is-live=true \
      ! video/x-raw,width=1280,height=720,framerate=30/1 \
      ! textoverlay text="MPEG-TS over RTP - ST 2022-2 (pt 33)" valignment=top halignment=center font-desc="Sans Bold 24" shaded-background=true \
      ! clockoverlay valignment=bottom halignment=right time-format="%H:%M:%S" font-desc="Sans Bold 18" shaded-background=true \
      ! videoconvert ! video/x-raw,format=I420 \
      ! x264enc bitrate=$BITRATE speed-preset=veryfast tune=zerolatency key-int-max=30 \
      ! h264parse config-interval=-1 ! queue ! mux. \
    audiotestsrc wave=ticks freq=500 volume=0.2 tick-interval=2000000000 is-live=true \
      ! audio/x-raw,format=S16LE,rate=48000,channels=2 \
      ! audioconvert ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux. \
    mpegtsmux name=mux alignment=7 \
      ! rtpmp2tpay \
      ! udpsink host=$TSRTP_GRP port=$TSRTP_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(tsrtp sender dropped -- restart in 2s)"; sleep 2
done
