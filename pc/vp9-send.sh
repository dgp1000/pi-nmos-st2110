#!/usr/bin/env bash
# Atoll VP9 source: royalty-free video as an RTP flow (RFC 7741, rtpvp9pay) on the island.
# The only feed here encoded on the CPU -- the 2080 Ti is Turing, which DECODES VP9 in hardware
# (nvvp9dec) but cannot encode it -- so this is the one flow that costs cores instead of NVENC
# sessions, which is useful precisely because NVENC sessions are the scarcer resource.
#
# deadline=1 selects libvpx realtime mode; without it vp9enc defaults to a quality deadline that is
# nowhere near realtime. cpu-used trades quality for speed (higher = faster). Benchmarked at 1.8x
# realtime for 720p30 on 20 cores, so there is headroom, but this is the feed to drop first if the
# box ever gets busy.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, VP9_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
BITRATE="${VP9_BITRATE:-4000000}"   # bits/s
THREADS="${VP9_THREADS:-8}"
echo "vp9-send: VP9 over RTP (RFC 7741, libvpx realtime) -> $VP9_GRP:$VP9_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q videotestsrc pattern=bar is-live=true \
    ! video/x-raw,width=1280,height=720,framerate=30/1 \
    ! textoverlay text="VP9 over RTP - RFC 7741 (libvpx, CPU encode)" valignment=top halignment=center font-desc="Sans Bold 24" shaded-background=true \
    ! clockoverlay valignment=bottom halignment=right time-format="%H:%M:%S" font-desc="Sans Bold 18" shaded-background=true \
    ! videoconvert ! video/x-raw,format=I420 \
    ! vp9enc deadline=1 cpu-used=8 threads=$THREADS target-bitrate=$BITRATE lag-in-frames=0 \
        keyframe-max-dist=30 end-usage=cbr error-resilient=1 \
    ! rtpvp9pay pt=96 \
    ! udpsink host=$VP9_GRP port=$VP9_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(vp9 sender dropped -- restart in 2s)"; sleep 2
done
