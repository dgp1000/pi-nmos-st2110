#!/usr/bin/env bash
# Atoll display as an MJPEG stream to stdout, FOLLOWING the panel switcher.
#   multiview-mjpeg.sh <boundary> <layout> <active> <slots>
#     layout: single | side | multi     active: hevc|jxs|raw|music
#     slots:  4 comma-sep source keys for multi (e.g. hevc,raw,jxs,music)
# Same sources as output-render.sh, but the display sink is jpegenc->multipartmux->fdsink
# (no WSLg window needed). Every tile is VIDEO-ONLY: a tile that demuxes an absent audio
# track stalls the whole compositor (the music placeholder is video-only).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"
IFACE="${ISLAND_IFACE:-eth0}"
B="${1:-atollframe}"; LAYOUT="${2:-multi}"; ACTIVE="${3:-hevc}"; SLOTS="${4:-hevc,raw,jxs,music}"
RAW_CAPS="application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,colorimetry=(string)BT601-5,payload=(int)96"
F="Sans Bold 22"
TAIL="videoscale ! video/x-raw,width=1280,height=720 ! videoconvert ! jpegenc quality=72 ! multipartmux boundary=$B ! fdsink fd=1"

# group/port + label for a source key
grpof() { case "$1" in hevc) echo "$HEVC_GRP $HEVC_PORT";; jxs) echo "$HOME_GRP $HOME_PORT";; music) echo "$MUSIC_GRP $MUSIC_PORT";; esac; }
labof() { case "$1" in hevc) echo "Live TV";; jxs) echo "Home videos";; music) echo "Music";; raw) echo "Pi raw 2110-20";; esac; }

# full-frame decode of the active source (for single), no scaling to a fixed size
src_full() {
  if [ "$1" = raw ]; then
    echo "udpsrc address=$PI_RAW_GRP port=$PI_RAW_PORT multicast-iface=$IFACE auto-multicast=true caps=\"$RAW_CAPS\" ! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080"
  else
    read g p < <(grpof "$1")
    echo "udpsrc address=$g port=$p multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080"
  fi
}

# one tile (video-only) wired to mix.sink_<idx>, scaled to WxH, with a label
tile() {   # $1=key $2=w $3=h $4=idx
  local key="$1" w="$2" h="$3" idx="$4" lab; lab="$(labof "$key")"
  if [ "$key" = raw ]; then
    echo "udpsrc address=$PI_RAW_GRP port=$PI_RAW_PORT multicast-iface=$IFACE auto-multicast=true caps=\"$RAW_CAPS\" ! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$w,height=$h ! textoverlay text=\"$lab\" valignment=top halignment=left xpad=14 ypad=10 font-desc=\"$F\" shaded-background=true ! mix.sink_$idx"
  else
    read g p < <(grpof "$key")
    echo "udpsrc address=$g port=$p multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux ! h265parse ! queue ! nvh265dec ! cudadownload ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$w,height=$h ! textoverlay text=\"$lab\" valignment=top halignment=left xpad=14 ypad=10 font-desc=\"$F\" shaded-background=true ! mix.sink_$idx"
  fi
}

case "$LAYOUT" in
  single)
    exec gst-launch-1.0 -q -e $(src_full "$ACTIVE") ! $TAIL
    ;;
  side)
    exec gst-launch-1.0 -q -e \
      compositor name=mix ignore-inactive-pads=true background=black sink_0::xpos=0 sink_0::ypos=270 sink_1::xpos=960 sink_1::ypos=270 \
        ! video/x-raw,width=1920,height=1080,framerate=30/1 ! videoconvert ! $TAIL \
      $(tile hevc 960 540 0) \
      $(tile raw 960 540 1)
    ;;
  *)  # multi (2x2)
    IFS=',' read -r m0 m1 m2 m3 <<< "$SLOTS"
    exec gst-launch-1.0 -q -e \
      compositor name=mix ignore-inactive-pads=true background=black sink_0::xpos=0 sink_0::ypos=0 sink_1::xpos=960 sink_1::ypos=0 sink_2::xpos=0 sink_2::ypos=540 sink_3::xpos=960 sink_3::ypos=540 \
        ! video/x-raw,width=1920,height=1080,framerate=30/1 ! videoconvert \
        ! clockoverlay halignment=center valignment=position ypos=0.35 time-format="%H:%M:%S" font-desc="Sans Bold 24" shaded-background=true \
        ! textoverlay text="Pi5 PTP GM" halignment=center valignment=position ypos=0.405 font-desc="Sans Bold 12" color=0xc8ffffff shaded-background=false \
        ! $TAIL \
      $(tile "${m0:-hevc}" 960 540 0) \
      $(tile "${m1:-raw}"  960 540 1) \
      $(tile "${m2:-jxs}"  960 540 2) \
      $(tile "${m3:-music}" 960 540 3)
    ;;
esac
