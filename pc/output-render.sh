#!/usr/bin/env bash
# Unified native OUTPUT renderer for the IS-05 panel, driven from the iPad.
# Polls the panel /state -> {active, layout} and renders on a monitor; relaunches the
# GStreamer pipeline only when the relevant key changes.
#
#   layout=single -> the active source fullscreen (follows IS-05 takes)
#   layout=side   -> PC HEVC 4K | Pi raw 2110-20, side by side
#   layout=multi  -> 2x2 mosaic: HEVC 4K, Pi raw, Home videos (5008), clock
#
# Sources: HEVC on 239.10.10.65:5010 and Home videos on 239.10.10.22:5008 are both HEVC/TS
# (gst-decodable -> real tiles); Pi raw is RTP 2110-20 on 239.10.10.20:5005.
#
# Run in a LOCAL WSL terminal:  bash pc/output-render.sh [SCREEN]   (SCREEN=2 default; 0=primary)
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"
IFACE="${HEVC_IFACE:-eth0}"
PANEL="${PANEL:-http://localhost:8096}"
SCREEN="${1:-2}"
export PULSE_SERVER="${PULSE_SERVER:-unix:/mnt/wslg/PulseServer}"
export GALLIUM_DRIVER="${GALLIUM_DRIVER:-d3d12}"
MOVER="$(wslpath -w "$DIR/move-window-screen.ps1")"
RAW_CAPS="application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,payload=(int)96"
HEVC_GRP=239.10.10.65; HEVC_PORT=5010
HOME_GRP=239.10.10.22; HOME_PORT=5008      # "PC JPEG-XS" slot, now HEVC home videos
MUSIC_GRP=239.10.10.30; MUSIC_PORT=5012    # "Now Playing" music channel (Mac page, bridged onto the island)
F="Sans Bold 22"
# Semi-transparent "ATOLL" bug, top-right of every output (single/side/multi). color is
# 0xAARRGGBB -> 0x80 = ~50% white; no shaded box so it reads as a transparent watermark.
BRAND="textoverlay text=ATOLL valignment=top halignment=right ypad=18 xpad=28 font-desc='Sans Bold 20' color=0x80ffffff shaded-background=false"

# HEVC decode -> tile WxH (no sink). $1=group $2=port $3=w $4=h $5=tsdemux-name.
hevc_tile() { echo "udpsrc address=$1 port=$2 multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=$5 $5. ! h265parse ! queue ! nvh265dec ! cudadownload ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$3,height=$4"; }
# HEVC fullscreen viewer (video + MP3 audio). $1=group $2=port.
hevc_full() { echo "gst-launch-1.0 -q udpsrc address=$1 port=$2 multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=d d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080 ! videoconvert ! $BRAND ! waylandsink fullscreen=true sync=true d. ! mpegaudioparse ! queue ! mpg123audiodec ! audioconvert ! audioresample ! autoaudiosink sync=true"; }
raw_video()  { echo "udpsrc address=239.10.10.20 port=5005 multicast-iface=$IFACE auto-multicast=true caps='$RAW_CAPS' ! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert ! videoscale ! video/x-raw,width=$1,height=$2"; }

# Audio-only pipeline for the SELECTED source (used in side/multi, and single+raw); the
# video pad is parsed-then-dropped so we don't waste a decode. hevc/home carry MP3 in the
# TS; Pi raw's audio is the separate ST 2110-30 L24 flow on 239.10.10.10:5004.
audio_cmd() {   # $1 = active source
  case "$1" in
    hevc) echo "gst-launch-1.0 -q udpsrc address=$HEVC_GRP port=$HEVC_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! mpegaudioparse ! mpg123audiodec ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
    jxs)  echo "gst-launch-1.0 -q udpsrc address=$HOME_GRP port=$HOME_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! mpegaudioparse ! mpg123audiodec ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
    music) echo "gst-launch-1.0 -q udpsrc address=$MUSIC_GRP port=$MUSIC_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! mpegaudioparse ! mpg123audiodec ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
    raw)  echo "gst-launch-1.0 -q udpsrc address=239.10.10.10 port=5004 multicast-iface=$IFACE auto-multicast=true buffer-size=16777216 caps='application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96' ! rtpjitterbuffer latency=500 ! rtpL24depay ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
  esac
}

build_pipeline() {   # $1=layout  $2=active
  case "$1" in
    single)
      case "$2" in
        hevc)  hevc_full "$HEVC_GRP" "$HEVC_PORT" ;;
        jxs)   hevc_full "$HOME_GRP" "$HOME_PORT" ;;
        music) hevc_full "$MUSIC_GRP" "$MUSIC_PORT" ;;
        raw)  echo "gst-launch-1.0 -q udpsrc address=239.10.10.20 port=5005 multicast-iface=$IFACE auto-multicast=true caps='$RAW_CAPS' ! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert ! videoscale ! $BRAND ! waylandsink fullscreen=true sync=false" ;;
      esac ;;
    side)
      echo "gst-launch-1.0 -e compositor name=mix ignore-inactive-pads=true background=black sink_0::xpos=0 sink_0::ypos=270 sink_1::xpos=960 sink_1::ypos=270 ! video/x-raw,width=1920,height=1080 ! videoconvert ! $BRAND ! waylandsink fullscreen=true sync=false \
        $(hevc_tile "$HEVC_GRP" "$HEVC_PORT" 960 540 hd) ! textoverlay text='PC HEVC 4K' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_0 \
        hd. ! mpegaudioparse ! fakesink sync=false \
        $(raw_video 960 540) ! textoverlay text='Pi raw 2110-20' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_1" ;;
    multi)
      echo "gst-launch-1.0 -e compositor name=mix ignore-inactive-pads=true background=black sink_0::xpos=0 sink_0::ypos=0 sink_1::xpos=960 sink_1::ypos=0 sink_2::xpos=0 sink_2::ypos=540 sink_3::xpos=960 sink_3::ypos=540 ! video/x-raw,width=1920,height=1080 ! videoconvert ! clockoverlay valignment=top halignment=left time-format='%H:%M:%S' font-desc='Sans Bold 22' shaded-background=true ypad=14 xpad=16 ! textoverlay text='Pi5 PTP GM' valignment=top halignment=left ypad=48 xpad=18 font-desc='Sans Bold 12' color=0xc8ffffff shaded-background=false ! $BRAND ! waylandsink fullscreen=true sync=false \
        $(hevc_tile "$HEVC_GRP" "$HEVC_PORT" 960 540 hd) ! textoverlay text='PC HEVC 4K' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_0 \
        hd. ! mpegaudioparse ! fakesink sync=false \
        $(raw_video 960 540) ! textoverlay text='Pi raw 2110-20' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_1 \
        $(hevc_tile "$HOME_GRP" "$HOME_PORT" 960 540 jd) ! textoverlay text='Home videos' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_2 \
        jd. ! mpegaudioparse ! fakesink sync=false \
        $(hevc_tile "$MUSIC_GRP" "$MUSIC_PORT" 960 540 md) ! textoverlay text='Music' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_3" ;;
  esac
}

cur_key=""
pid=""
apid=""
aud_key="__init__"
kill_view()  { [ -n "$pid" ]  && kill -- -"$pid"  2>/dev/null; pid=""; }
kill_audio() { [ -n "$apid" ] && kill -- -"$apid" 2>/dev/null; apid=""; }
trap 'kill_view; kill_audio; exit 0' INT TERM EXIT

echo "output-render: following $PANEL/state -> monitor $SCREEN (Ctrl+C to stop)"
while true; do
  resp="$(curl -s --max-time 2 "$PANEL/state" || true)"
  active="$(printf '%s' "$resp" | sed -n 's/.*"active"[: ]*"\([a-z]*\)".*/\1/p')"
  layout="$(printf '%s' "$resp" | sed -n 's/.*"layout"[: ]*"\([a-z]*\)".*/\1/p')"
  [ -z "$layout" ] && layout="single"
  [ -z "$active" ] && { sleep 1; continue; }

  # --- video: relaunch the pipeline only on layout/source change ---
  if [ "$layout" = "single" ]; then key="single:$active"; else key="$layout"; fi
  if [ "$key" != "$cur_key" ]; then
    cmd="$(build_pipeline "$layout" "$active")"
    if [ -n "$cmd" ]; then
      echo "$(date +%T) render -> $key"
      kill_view
      setsid bash -c "$cmd" >/tmp/output-view.log 2>&1 &
      pid=$!
      cur_key="$key"
      [ "$SCREEN" != "0" ] && powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$MOVER" -Screen "$SCREEN" -TimeoutSec 12 >/dev/null 2>&1 &
    fi
  fi

  # --- audio: follow the SELECTED source. single hevc/jxs already embed their own
  # (lip-synced) audio; run the standalone follower only where the video has none:
  # side/multi, and single+raw. Switches instantly without touching the video. ---
  if [ "$layout" = "single" ] && [ "$active" != "raw" ]; then akey=""; else akey="$active"; fi
  if [ "$akey" != "$aud_key" ]; then
    kill_audio
    if [ -n "$akey" ]; then
      acmd="$(audio_cmd "$akey")"
      [ -n "$acmd" ] && { setsid bash -c "$acmd" >/tmp/output-audio.log 2>&1 & apid=$!; }
    fi
    aud_key="$akey"
    echo "$(date +%T) audio -> ${akey:-embedded}"
  fi
  sleep 1
done
