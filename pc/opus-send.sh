#!/usr/bin/env bash
# Atoll Opus source: a compressed AUDIO essence carried as its own RTP flow (RFC 7587, rtpopuspay)
# on the island. This is the audio partner of the H.264 bars feed -- two separate RTP essence flows
# for one programme, mirroring how ST 2110 splits video (-20) from audio (-30), but with modern
# compressed codecs instead of uncompressed. Together they are a "bars and tone" reference.
#
# The tone is a 1 kHz pip once a second (every 5th emphasised), not a continuous tone: it keeps the
# VU meters moving, is far easier to live with, and doubles as a lip-sync / latency reference you can
# see against the clock burned into the H.264 video.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, OPUS_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
BITRATE="${OPUS_BITRATE:-96000}"   # bits/s; Opus is transparent well below the AAC/MP3 rates here
echo "opus-send: Opus audio over RTP (RFC 7587) -> $OPUS_GRP:$OPUS_PORT on $IFACE  (Ctrl+C to stop)"
while true; do
  gst-launch-1.0 -q audiotestsrc wave=ticks freq=1000 volume=0.2 \
      tick-interval=1000000000 marker-tick-period=5 marker-tick-volume=0.5 is-live=true \
    ! audio/x-raw,format=S16LE,rate=48000,channels=2 \
    ! audioconvert ! audioresample \
    ! opusenc bitrate=$BITRATE \
    ! rtpopuspay pt=97 \
    ! udpsink host=$OPUS_GRP port=$OPUS_PORT multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(opus sender dropped -- restart in 2s)"; sleep 2
done
