#!/usr/bin/env bash
# Atoll Motion JPEG source: every frame a standalone JPEG, carried as RTP (RFC 2435, rtpjpegpay).
# The oldest video-over-RTP payload format still in daily use (IP cameras, KVM, medical/industrial),
# and the only ALL-INTRA compressed flow on the island besides JPEG 2000 -- no inter-frame prediction,
# so any packet loss costs exactly one frame and a receiver can join on any frame, not just an IDR.
# Motion costs nothing either: bitrate is the same whether the picture moves or not (hence the ball).
#
# RFC 2435 limits: baseline JPEG, 4:2:0/4:2:2, and dimensions carried in 8-pixel units in a byte, so
# max 2040x2040 -- 1280x720 (160x90 units) is fine. QUALITY is the bitrate knob: this is all-intra, so
# it runs an order of magnitude above the HEVC channels and is the one feed that can actually threaten
# the WSL bridge's packet-rate ceiling. 60 measured ~13 Mbit/s (~1.2k pps), comfortably under the
# ~4k pps that is known to work. Raise with care; verify pps, not just Mbit/s.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, MJPEG_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
QUALITY="${MJPEG_QUALITY:-60}"
echo "mjpeg-send: Motion JPEG over RTP (RFC 2435, q=$QUALITY) -> $MJPEG_GRP:$MJPEG_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q videotestsrc pattern=ball motion=sweep is-live=true \
    ! video/x-raw,width=1280,height=720,framerate=30/1 \
    ! clockoverlay valignment=bottom halignment=right time-format="%H:%M:%S" font-desc="Sans Bold 18" shaded-background=true \
    ! videoconvert ! video/x-raw,format=I420 \
    ! jpegenc quality=$QUALITY \
    ! rtpjpegpay pt=96 \
    ! udpsink host=$MJPEG_GRP port=$MJPEG_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(mjpeg sender dropped -- restart in 2s)"; sleep 2
done
