#!/usr/bin/env bash
# Atoll ST 2022-1 FEC source: MPEG-TS over RTP protected by SMPTE 2022-1 forward error correction.
# Three flows go out, the ST 2022-1 convention of media on port P and FEC on P+2 / P+4:
#   P    media   (the same TS-over-RTP as the tsrtp feed)
#   P+2  column FEC
#   P+4  row FEC
# A 2D (columns x rows) matrix lets the receiver rebuild packets it never received, with no
# retransmission and no added latency beyond the matrix depth -- which is why broadcast uses it on
# one-way multicast where TCP-style recovery is impossible.
#
# GOTCHA: rtpst2022-1-fecenc rejects anything whose SSRC is not 0 ("Chained buffer must have
# SSRC == 0"), and rtpmp2tpay picks a random SSRC by default -- hence the explicit ssrc=0.
#
# Overhead is 1 packet per column + 1 per row: at columns=10 rows=5 that is 15 FEC packets per 50
# media packets, ~30%. MEASURED behaviour: FEC recovers well at low, sporadic loss (~2-5%, its design
# point and what real networks actually do) and is progressively overwhelmed above ~10%, where too
# many packets are missing from each matrix to reconstruct. Tune the demo in that low range.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"        # ISLAND_IFACE, FEC_*, MCAST_TTL
IFACE="${ISLAND_IFACE:-eth0}"
BITRATE="${FEC_BITRATE:-3000}"
COLS="${FEC_COLUMNS:-10}"
ROWS="${FEC_ROWS:-5}"
COL_PORT=$((FEC_PORT + 2))
ROW_PORT=$((FEC_PORT + 4))
echo "fec-send: ST 2022-1 protected TS/RTP -> $FEC_GRP media:$FEC_PORT col:$COL_PORT row:$ROW_PORT on $IFACE"
while true; do
  gst-launch-1.0 -q \
    videotestsrc pattern=ball motion=sweep is-live=true \
      ! video/x-raw,width=1280,height=720,framerate=30/1 \
      ! clockoverlay valignment=bottom halignment=right time-format="%H:%M:%S" font-desc="Sans Bold 18" shaded-background=true \
      ! videoconvert ! video/x-raw,format=I420 \
      ! x264enc bitrate=$BITRATE speed-preset=veryfast tune=zerolatency key-int-max=1 \
      ! h264parse config-interval=-1 ! queue ! mux. \
    audiotestsrc wave=ticks freq=800 volume=0.2 tick-interval=1000000000 is-live=true \
      ! audio/x-raw,format=S16LE,rate=48000,channels=2 \
      ! audioconvert ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux. \
    mpegtsmux name=mux alignment=7 \
      ! rtpmp2tpay ssrc=0 \
      ! rtpst2022-1-fecenc name=fec columns=$COLS rows=$ROWS enable-column-fec=true enable-row-fec=true pt=96 \
    fec.src   ! queue ! udpsink host=$FEC_GRP port=$FEC_PORT  multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL \
    fec.fec_0 ! queue ! udpsink host=$FEC_GRP port=$COL_PORT  multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL \
    fec.fec_1 ! queue ! udpsink host=$FEC_GRP port=$ROW_PORT  multicast-iface="$IFACE" auto-multicast=true ttl=$MCAST_TTL
  echo "(fec sender dropped -- restart in 2s)"; sleep 2
done
