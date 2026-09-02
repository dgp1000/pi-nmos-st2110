#!/usr/bin/env bash
# Atoll ST 2022-7 Seamless Protection Switching source.
#
# The mechanism broadcast uses for genuine hitless redundancy: the SAME RTP stream is sent down two
# independent paths, and the receiver merges them by sequence number, taking whichever copy of each
# packet arrives first. Lose a path entirely and not one packet goes missing -- there is no failover
# event, no reconvergence, no glitch, because nothing ever had to switch.
#
# The critical property is that both copies must be BIT-IDENTICAL at the RTP layer: same SSRC, same
# sequence numbers, same timestamps. That is why this duplicates with a tee AFTER the payloader
# rather than running two encoders -- two encoders would produce two unrelated RTP streams that no
# receiver could reconcile. ssrc is pinned for the same reason.
#
# HONEST LIMITATION: a real facility runs the two paths over physically separate networks (the
# classic red/blue LANs) so that a switch, cable or NIC failure can only take one down. This rig has
# a single island L2, so the two paths are two multicast groups sharing it -- the merge logic and the
# seamless behaviour are exactly the same, but the fault isolation is simulated (kill a path from the
# panel) rather than physical.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, SPS_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
BITRATE="${SPS_BITRATE:-3000}"
echo "sps-send: ST 2022-7 dual-path -> A $SPS_A_GRP:$SPS_A_PORT  B $SPS_B_GRP:$SPS_B_PORT on $IFACE"
while true; do
  gst-launch-1.0 -q \
    videotestsrc pattern=ball motion=sweep is-live=true \
      ! video/x-raw,width=1280,height=720,framerate=30/1 \
      ! clockoverlay valignment=bottom halignment=right time-format="%H:%M:%S" font-desc="Sans Bold 18" shaded-background=true \
      ! videoconvert ! video/x-raw,format=I420 \
      ! x264enc bitrate=$BITRATE speed-preset=veryfast tune=zerolatency key-int-max=30 \
      ! h264parse config-interval=-1 ! queue ! mux. \
    audiotestsrc wave=ticks freq=1200 volume=0.2 tick-interval=1000000000 is-live=true \
      ! audio/x-raw,format=S16LE,rate=48000,channels=2 \
      ! audioconvert ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux. \
    mpegtsmux name=mux alignment=7 \
      ! rtpmp2tpay ssrc=2022 \
      ! tee name=t \
    t. ! queue ! udpsink host=$SPS_A_GRP port=$SPS_A_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL \
    t. ! queue ! udpsink host=$SPS_B_GRP port=$SPS_B_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(sps sender dropped -- restart in 2s)"; sleep 2
done
