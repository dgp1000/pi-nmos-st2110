#!/usr/bin/env bash
# Unified native OUTPUT renderer for the IS-05 panel, driven from the iPad.
# Polls the panel /state -> {active, layout} and renders on a monitor; relaunches the
# GStreamer pipeline only when the relevant key changes.
#
#   layout=single -> the active source fullscreen (follows IS-05 takes)
#   layout=side   -> Live TV | Pi raw 2110-20, side by side
#   layout=multi  -> 2x2 mosaic: HEVC 4K, Pi raw, Home videos (5008), clock
#
# Sources: HEVC on 239.10.10.65:5010 and Home videos on 239.10.10.22:5008 are both HEVC/TS
# (gst-decodable -> real tiles); Pi raw is RTP 2110-20 on 239.10.10.20:5005.
#
# Run in a LOCAL WSL terminal:  bash pc/output-render.sh [SCREEN]   (SCREEN=2 default; 0=primary)
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/atoll.conf"             # groups (HEVC/HOME/MUSIC/REELS/PI_RAW/PI_AUDIO), ISLAND_IFACE, PANEL_PORT
IFACE="${ISLAND_IFACE:-eth0}"
PANEL="${PANEL:-http://localhost:$PANEL_PORT}"
SCREEN="${1:-2}"
# GALLIUM_DRIVER + PULSE_SERVER come from atoll.conf (WSL only). The window-mover is WSL-only too.
MOVER=""
[ "${ATOLL_PLATFORM:-}" = wsl ] && MOVER="$(wslpath -w "$DIR/move-window-screen.ps1")"
RAW_CAPS="application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,colorimetry=(string)BT601-5,payload=(int)96"
F="Sans Bold 22"
# Semi-transparent "ATOLL" bug, top-right of every output (single/side/multi). color is
# 0xAARRGGBB -> 0x80 = ~50% white; no shaded box so it reads as a transparent watermark.
BRAND="textoverlay text=ATOLL valignment=top halignment=right ypad=18 xpad=28 font-desc='Sans Bold 20' color=0x80ffffff shaded-background=false"

# HEVC decode -> tile WxH (no sink). $1=group $2=port $3=w $4=h $5=tsdemux-name.
hevc_tile() { echo "udpsrc address=$1 port=$2 multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=$5 $5. ! h265parse ! queue ! nvh265dec ! cudadownload ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$3,height=$4"; }
# HEVC fullscreen viewer (video + MP3 audio). $1=group $2=port.
hevc_full() { echo "gst-launch-1.0 -q udpsrc address=$1 port=$2 multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=d d. ! h265parse ! queue ! nvh265dec ! cudadownload ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080 ! videoconvert ! $BRAND ! $VIDEO_SINK sync=true d. ! mpegaudioparse ! queue ! mpg123audiodec ! audioconvert ! audioresample ! autoaudiosink sync=true"; }
raw_video()  { echo "udpsrc address=$PI_RAW_GRP port=$PI_RAW_PORT multicast-iface=$IFACE auto-multicast=true caps='$RAW_CAPS' ! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert ! videoscale ! video/x-raw,width=$1,height=$2"; }

# One 2x2 multiview tile for a source key, wired to mix.sink_<idx> with a label. HEVC tiles also
# terminate their TS audio pad (an unlinked pad would error). $1=src $2=idx $3=w $4=h
tile_full() {
  local src=$1 idx=$2 w=$3 h=$4 name="t$2" grp port label
  case "$src" in
    raw)   echo "$(raw_video "$w" "$h") ! textoverlay text='Pi raw 2110-20' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx"; return ;;
    jpegxs) echo "videotestsrc pattern=ball is-live=true ! video/x-raw,width=$w,height=$h,framerate=30/1 ! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert ! textoverlay text='JPEG XS 2110-22' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx"; return ;;
    j2k) echo "udpsrc address=$J2K_GRP port=$J2K_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 caps='application/x-rtp,media=video,encoding-name=JPEG2000,clock-rate=90000,sampling=YCbCr-4:2:0' ! rtpj2kdepay ! avdec_jpeg2000 ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$w,height=$h ! textoverlay text='JPEG 2000' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx"; return ;;
    hevc)  grp=$HEVC_GRP;  port=$HEVC_PORT;  label='Live TV' ;;
    jxs)   grp=$HOME_GRP;  port=$HOME_PORT;  label='Home videos' ;;
    music) grp=$MUSIC_GRP; port=$MUSIC_PORT; label='Music' ;;
    reels) grp=$REELS_GRP; port=$REELS_PORT; label='Test Reels' ;;
    *)     grp=$HEVC_GRP;  port=$HEVC_PORT;  label="$src" ;;
  esac
  local abranch=" $name. ! queue ! mpegaudioparse ! fakesink sync=false"
  [ "$src" = music ] && abranch=""   # video-only placeholder: no TS audio pad to terminate
  echo "$(hevc_tile "$grp" "$port" "$w" "$h" "$name") ! textoverlay text='$label' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx$abranch"
}

# Audio-only pipeline for the SELECTED source (used in side/multi, and single+raw); the
# video pad is parsed-then-dropped so we don't waste a decode. hevc/home carry MP3 in the
# TS; Pi raw's audio is the separate ST 2110-30 L24 flow on 239.10.10.10:5004.
audio_cmd() {   # $1 = active source
  case "$1" in
    hevc) echo "gst-launch-1.0 -q udpsrc address=$HEVC_GRP port=$HEVC_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! mpegaudioparse ! mpg123audiodec ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
    jxs)  echo "gst-launch-1.0 -q udpsrc address=$HOME_GRP port=$HOME_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! mpegaudioparse ! mpg123audiodec ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
    music) echo "gst-launch-1.0 -q udpsrc address=$MUSIC_GRP port=$MUSIC_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! mpegaudioparse ! mpg123audiodec ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
    reels) echo "gst-launch-1.0 -q udpsrc address=$REELS_GRP port=$REELS_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! mpegaudioparse ! mpg123audiodec ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
    raw)  echo "gst-launch-1.0 -q udpsrc address=$PI_AUDIO_GRP port=$PI_AUDIO_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=16777216 caps='application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96' ! rtpjitterbuffer latency=500 ! rtpL24depay ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000" ;;
  esac
}

build_pipeline() {   # $1=layout  $2=active
  case "$1" in
    single)
      case "$2" in
        hevc)  hevc_full "$HEVC_GRP" "$HEVC_PORT" ;;
        jxs)   hevc_full "$HOME_GRP" "$HOME_PORT" ;;
        music) hevc_full "$MUSIC_GRP" "$MUSIC_PORT" ;;
        reels) hevc_full "$REELS_GRP" "$REELS_PORT" ;;
        jpegxs) echo "gst-launch-1.0 -q videotestsrc pattern=ball is-live=true ! video/x-raw,width=1280,height=720,framerate=30/1 ! textoverlay text='JPEG XS 2110-22 codec' valignment=top halignment=center font-desc='$F' shaded-background=true ! videoconvert ! video/x-raw,format=Y42B ! svtjpegxsenc ! svtjpegxsdec ! videoconvert ! videoscale ! $BRAND ! $VIDEO_SINK sync=false" ;;
        j2k) echo "gst-launch-1.0 -q udpsrc address=$J2K_GRP port=$J2K_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 caps='application/x-rtp,media=video,encoding-name=JPEG2000,clock-rate=90000,sampling=YCbCr-4:2:0' ! rtpj2kdepay ! avdec_jpeg2000 ! videoconvert ! videoscale ! $BRAND ! $VIDEO_SINK sync=false" ;;
        raw)  echo "gst-launch-1.0 -q udpsrc address=$PI_RAW_GRP port=$PI_RAW_PORT multicast-iface=$IFACE auto-multicast=true caps='$RAW_CAPS' ! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert ! videoscale ! $BRAND ! $VIDEO_SINK sync=false" ;;
      esac ;;
    side)
      echo "gst-launch-1.0 -e compositor name=mix ignore-inactive-pads=true background=black sink_0::xpos=0 sink_0::ypos=270 sink_1::xpos=960 sink_1::ypos=270 ! video/x-raw,width=1920,height=1080 ! videoconvert ! $BRAND ! $VIDEO_SINK sync=false \
        $(hevc_tile "$HEVC_GRP" "$HEVC_PORT" 960 540 hd) ! textoverlay text='Live TV' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_0 \
        hd. ! mpegaudioparse ! fakesink sync=false \
        $(raw_video 960 540) ! textoverlay text='Pi raw 2110-20' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_1" ;;
    multi)
      # $3 = slots "tl,tr,bl,br" (any source -> any of the four 960x540 quadrants).
      IFS=',' read -r m0 m1 m2 m3 <<< "${3:-hevc,raw,jxs,music}"
      echo "gst-launch-1.0 -e compositor name=mix ignore-inactive-pads=true background=black sink_0::xpos=0 sink_0::ypos=0 sink_1::xpos=960 sink_1::ypos=0 sink_2::xpos=0 sink_2::ypos=540 sink_3::xpos=960 sink_3::ypos=540 ! video/x-raw,width=1920,height=1080 ! videoconvert ! clockoverlay halignment=center valignment=position ypos=0.35 time-format='%H:%M:%S' font-desc='Sans Bold 24' shaded-background=true ! textoverlay text='Pi5 PTP GM' halignment=center valignment=position ypos=0.405 font-desc='Sans Bold 12' color=0xc8ffffff shaded-background=false ! $BRAND ! $VIDEO_SINK sync=false \
        $(tile_full "$m0" 0 960 540) \
        $(tile_full "$m1" 1 960 540) \
        $(tile_full "$m2" 2 960 540) \
        $(tile_full "$m3" 3 960 540)" ;;
  esac
}

cur_key=""
pid=""
apid=""
aud_key="__init__"
last_tvch=""
tv_settle=0
kill_view()  { [ -n "$pid" ]  && kill -- -"$pid"  2>/dev/null; pid=""; }
kill_audio() { [ -n "$apid" ] && kill -- -"$apid" 2>/dev/null; apid=""; }
trap 'kill_view; kill_audio; exit 0' INT TERM EXIT

echo "output-render: following $PANEL/state -> monitor $SCREEN (Ctrl+C to stop)"
while true; do
  resp="$(curl -s --max-time 2 "$PANEL/state" || true)"
  active="$(printf '%s' "$resp" | sed -n 's/.*"active"[: ]*"\([a-z0-9]*\)".*/\1/p')"
  layout="$(printf '%s' "$resp" | sed -n 's/.*"layout"[: ]*"\([a-z0-9]*\)".*/\1/p')"
  slots="$(printf '%s' "$resp" | sed -n 's/.*"slots"[: ]*"\([a-z0-9,]*\)".*/\1/p')"
  [ -z "$layout" ] && layout="single"
  [ -z "$slots" ]  && slots="hevc,raw,jxs,music"
  [ -z "$active" ] && { sleep 1; continue; }

  # --- recover on channel change / pipeline death (single gst-launch dies on a tile decode error) ---
  tvch="$(cat "$ATOLL_RUN/tv-channel" 2>/dev/null)"
  if [ "$tvch" != "$last_tvch" ]; then last_tvch="$tvch"; tv_settle=6; fi
  if [ "$tv_settle" -gt 0 ]; then tv_settle=$((tv_settle-1)); [ "$tv_settle" -eq 0 ] && cur_key="__force_rebuild__"; fi
  # --- video: relaunch the pipeline only on layout/source/tile-assignment change ---
  case "$layout" in single) key="single:$active";; multi) key="multi:$slots";; *) key="$layout";; esac
  if [ "$key" != "$cur_key" ]; then
    cmd="$(build_pipeline "$layout" "$active" "$slots")"
    if [ -n "$cmd" ]; then
      echo "$(date +%T) render -> $key"
      kill_view
      setsid bash -c "$cmd" >/tmp/output-view.log 2>&1 &
      pid=$!
      cur_key="$key"
      [ "${ATOLL_PLATFORM:-}" = wsl ] && [ "$SCREEN" != "0" ] && powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$MOVER" -Screen "$SCREEN" -TimeoutSec 12 >/dev/null 2>&1 &
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
