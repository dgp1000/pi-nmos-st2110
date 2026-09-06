#!/usr/bin/env bash
# Atoll IS-08 audio channel-mapping PROCESSOR. Receives the music channel's pre-map L24 audio
# on localhost (music-channel.sh sends it there), applies the channel-routing matrix set by the
# IS-08 Channel Mapping API (audiomap-nmos.py writes ~/atoll-run/audiomap), and re-sends L24 on
# the real MUSIC_AUDIO_GRP that the renderer plays. Because only this tiny hop restarts on a map
# change, re-routing is instant and never disturbs the music VIDEO tile (no compositor stall).
# The matrix is a gst audiomixmatrix out x in routing matrix (one 1.0 per output row, or a
# zero row for silence) -- IS-08 is channel ROUTING, not mixing.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, MUSIC_AUDIO_*, MUSIC_AUDIO_PREMAP_PORT, ATOLL_RUN, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
PREMAP_PORT="${MUSIC_AUDIO_PREMAP_PORT:-5015}"
MAPFILE="${ATOLL_RUN:-$HOME/atoll-run}/audiomap"
DEFAULT_MATRIX="<<1.0,0.0>,<0.0,1.0>>"   # identity / straight stereo

INCAPS="application/x-rtp,media=(string)audio,clock-rate=(int)48000,encoding-name=(string)L24,channels=(int)2,payload=(int)96"

echo "audiomapper: localhost:$PREMAP_PORT -> matrix -> $MUSIC_AUDIO_GRP:$MUSIC_AUDIO_PORT (map: $MAPFILE)"
while true; do
  MATRIX="$DEFAULT_MATRIX"
  [ -r "$MAPFILE" ] && MATRIX="$(cat "$MAPFILE")"
  [ -z "$MATRIX" ] && MATRIX="$DEFAULT_MATRIX"
  echo "$(date +%T) applying matrix: $MATRIX"
  gst-launch-1.0 -q \
    udpsrc address=127.0.0.1 port="$PREMAP_PORT" caps="$INCAPS" \
    ! rtpjitterbuffer latency=50 ! rtpL24depay ! audioconvert \
    ! audio/x-raw,channels=2,channel-mask="(bitmask)0x3" \
    ! audiomixmatrix in-channels=2 out-channels=2 channel-mask=0x3 matrix="$MATRIX" \
    ! audioconvert ! audioresample ! audio/x-raw,format=S24BE,rate=48000,channels=2 \
    ! rtpL24pay pt=96 min-ptime=1000000 max-ptime=1000000 \
    ! udpsink host="$MUSIC_AUDIO_GRP" port="$MUSIC_AUDIO_PORT" multicast-iface="$IFACE" auto-multicast=true ttl="$MCAST_TTL" \
    || true
  sleep 1
done
