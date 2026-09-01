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
    # H.264 elementary stream over RTP (RFC 6184) -- not TS like the HEVC tiles, so it depays from RTP
    # directly (jitterbuffer -> rtph264depay) and hardware-decodes on NVDEC.
    h264) echo "udpsrc address=$H264_GRP port=$H264_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 caps='application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96' ! rtpjitterbuffer latency=100 ! rtph264depay ! h264parse ! nvh264dec ! cudadownload ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$w,height=$h ! textoverlay text='H.264 RTP' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx"; return ;;
    # VP9 over RTP (RFC 7741). nvvp9dec needs width/height/profile/alignment in its sink caps and
    # rtpvp9depay does not supply them -- vp9parse fills them in, else the decoder fails to link.
    vp9) echo "udpsrc address=$VP9_GRP port=$VP9_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 caps='application/x-rtp,media=video,clock-rate=90000,encoding-name=VP9,payload=96' ! rtpjitterbuffer latency=100 ! rtpvp9depay ! vp9parse ! nvvp9dec ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$w,height=$h ! textoverlay text='VP9 RTP' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx"; return ;;
    # MPEG-TS over RTP (SMPTE ST 2022-2, static payload type 33): the TS carries a full A/V
    # programme, so depay -> tsdemux -> decode, and the audio pad must be drained. NOTE the queue on
    # that drain is REQUIRED here: "audio/mpeg ! fakesink" with no queue stalls the demuxer on this
    # RTP path and the video never prerolls (the bare-UDP HEVC tiles get away without it).
    tsrtp) echo "udpsrc address=$TSRTP_GRP port=$TSRTP_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 caps='application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33' ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=$name $name. ! h264parse ! queue ! nvh264dec ! cudadownload ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$w,height=$h ! textoverlay text='TS over RTP' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx $name. ! audio/mpeg ! queue ! fakesink sync=false"; return ;;
    # ST 2022-1 FEC: media + column + row flows recombined by rtpst2022-1-fecdec. The tile always
    # runs protected with no injected loss; the interactive break/repair demo lives in single view
    # (meter-view.py), which can tune the loss and FEC-gate knobs live.
    fec) echo "udpsrc address=$FEC_GRP port=$FEC_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 caps='application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33' ! rtpst2022-1-fecdec name=fd$idx udpsrc address=$FEC_GRP port=$((FEC_PORT+2)) multicast-iface=$IFACE auto-multicast=true caps='application/x-rtp,payload=96,clock-rate=90000' ! queue ! fd$idx.fec_0 udpsrc address=$FEC_GRP port=$((FEC_PORT+4)) multicast-iface=$IFACE auto-multicast=true caps='application/x-rtp,payload=96,clock-rate=90000' ! queue ! fd$idx.fec_1 fd$idx. ! rtpmp2tdepay ! tsdemux name=$name $name. ! h264parse ! queue ! nvh264dec ! cudadownload ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$w,height=$h ! textoverlay text='ST 2022-1 FEC' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx $name. ! audio/mpeg ! queue ! fakesink sync=false"; return ;;
    # Motion JPEG over RTP (RFC 2435): all-intra, so no parser and no keyframe wait -- depay straight
    # to the GPU JPEG decoder. Bigger socket buffer: whole JPEG frames, ~1.4kB packets.
    mjpeg) echo "udpsrc address=$MJPEG_GRP port=$MJPEG_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=16777216 caps='application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=96' ! rtpjitterbuffer latency=100 ! rtpjpegdepay ! nvjpegdec ! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! videoscale ! video/x-raw,width=$w,height=$h ! textoverlay text='MJPEG RTP' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx"; return ;;
    hevc)  grp=$HEVC_GRP;  port=$HEVC_PORT;  label='Live TV' ;;
    jxs)   grp=$HOME_GRP;  port=$HOME_PORT;  label='Home videos' ;;
    music) grp=$MUSIC_GRP; port=$MUSIC_PORT; label='Music' ;;
    reels) grp=$REELS_GRP; port=$REELS_PORT; label='Test Reels' ;;
    *)     grp=$HEVC_GRP;  port=$HEVC_PORT;  label="$src" ;;
  esac
  local abranch=" $name. ! audio/mpeg ! fakesink sync=false"
  [ "$src" = music ] && abranch=""   # video-only placeholder: no TS audio pad to terminate
  # Live TV (hevc) is the only tile whose source channel-switches, so its 5010 PTS leaps at a switch
  # (broadcast epoch <-> black-fallback running-time). The compositor can't re-align the tile across
  # that jump and drops it to ~1 fps. single-segment restamps this tile onto one continuous local
  # running-time, eating the discontinuity, so the compositor always sees a clean 30fps timeline. The
  # non-switching tiles (home/music) don't need it and are left untouched.
  local ss=""
  [ "$src" = hevc ] && ss="identity single-segment=true ! "
  echo "$(hevc_tile "$grp" "$port" "$w" "$h" "$name") ! ${ss}textoverlay text='$label' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! queue leaky=downstream max-size-buffers=2 ! mix.sink_$idx$abranch"
}

# Audio-only pipeline for the SELECTED source (used in side/multi, and single+raw); the
# video pad is parsed-then-dropped so we don't waste a decode. hevc/home carry MP3 in the
# TS; Pi raw's audio is the separate ST 2110-30 L24 flow on 239.10.10.10:5004.
audio_cmd() {   # $1 = active source
  case "$1" in
    hevc) echo "gst-launch-1.0 -q udpsrc address=$HEVC_GRP port=$HEVC_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! audio/mpeg ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! decodebin ! audioconvert ! audio/x-raw,channels=2 ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! $ASINK" ;;
    jxs)  echo "gst-launch-1.0 -q udpsrc address=$HOME_GRP port=$HOME_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! audio/mpeg ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! decodebin ! audioconvert ! audio/x-raw,channels=2 ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! $ASINK" ;;
    music) echo "gst-launch-1.0 -q udpsrc address=$MUSIC_GRP port=$MUSIC_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! audio/mpeg ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! decodebin ! audioconvert ! audio/x-raw,channels=2 ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! $ASINK" ;;
    reels) echo "gst-launch-1.0 -q udpsrc address=$REELS_GRP port=$REELS_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 ! tsdemux name=a a. ! queue ! h265parse ! fakesink sync=false a. ! audio/mpeg ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! decodebin ! audioconvert ! audio/x-raw,channels=2 ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! $ASINK" ;;
    raw)  echo "gst-launch-1.0 -q udpsrc address=$PI_AUDIO_GRP port=$PI_AUDIO_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=16777216 caps='application/x-rtp,media=audio,clock-rate=48000,encoding-name=L24,channels=2,payload=96' ! rtpjitterbuffer latency=500 ! rtpL24depay ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! $ASINK" ;;
    # TS-over-RTP carries its audio inside the transport stream, so depay+demux to reach it.
    tsrtp) echo "gst-launch-1.0 -q udpsrc address=$TSRTP_GRP port=$TSRTP_PORT multicast-iface=$IFACE auto-multicast=true buffer-size=8388608 caps='application/x-rtp,media=video,clock-rate=90000,encoding-name=MP2T,payload=33' ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! tsdemux name=a a. ! queue ! h264parse ! fakesink sync=false a. ! audio/mpeg ! queue max-size-time=1500000000 max-size-bytes=0 max-size-buffers=0 ! decodebin ! audioconvert ! audio/x-raw,channels=2 ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! $ASINK" ;;
    # H.264's audio is the separate Opus RTP essence flow (RFC 7587), not embedded in the video stream.
    h264) echo "gst-launch-1.0 -q udpsrc address=$OPUS_GRP port=$OPUS_PORT multicast-iface=$IFACE auto-multicast=true caps='application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=97' ! rtpjitterbuffer latency=200 ! rtpopusdepay ! opusdec ! audioconvert ! audioresample ! queue max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! $ASINK" ;;
  esac
}

build_pipeline() {   # $1=layout  $2=active
  case "$1" in
    single)
      # video+audio+VU meters, following the active source (Python: cairooverlay + level)
      echo "python3 \"$DIR/meter-view.py\" \"$2\" \"$SCREEN\""
      ;;
    side)
      echo "gst-launch-1.0 -e compositor name=mix ignore-inactive-pads=true background=black sink_0::xpos=0 sink_0::ypos=270 sink_1::xpos=960 sink_1::ypos=270 ! video/x-raw,width=1920,height=1080 ! videoconvert ! $BRAND ! $VIDEO_SINK sync=false \
        $(hevc_tile "$HEVC_GRP" "$HEVC_PORT" 960 540 hd) ! identity single-segment=true ! textoverlay text='Live TV' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_0 \
        hd. ! audio/mpeg ! fakesink sync=false \
        $(raw_video 960 540) ! textoverlay text='Pi raw 2110-20' valignment=top halignment=left xpad=14 ypad=10 font-desc='$F' shaded-background=true ! mix.sink_1" ;;
    wall)
      # Python 2x2 wall: same one-pipeline topology as multi, but with a live tally border on the
      # on-air tile, per-tile audio meters and per-tile bitrate -- none of which a gst-launch string
      # can update after it starts. Slot changes still rebuild us; tally follows takes live.
      echo "python3 \"$DIR/wall-view.py\" \"${3:-hevc,raw,jxs,music}\" \"$SCREEN\""
      ;;
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
ADELAY_FILE="$ATOLL_RUN/audio-delay-ms"
last_adelay="__init__"
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

  # --- self-heal: rebuild only if the render pipeline actually died. Channel changes no longer force
  # a rebuild — the input-selector tv-send keeps 5010 continuous, so the Live TV tile updates in place ---
  if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then pid=""; cur_key="__force_rebuild__"; fi
  # Same self-heal for the standalone audio follower: a 5010 stream discontinuity (e.g. an atoll-tv
  # restart / channel self-heal) can throw the tsdemux branch into a "not-linked" error and kill the
  # audio process while the active source is unchanged. Without this, audio stays dead until the user
  # switches sources; with it, the loop below relaunches the follower within ~1s. A fresh launch on a
  # settled stream links cleanly, so a transient blip recovers on its own.
  if [ -n "$apid" ] && ! kill -0 "$apid" 2>/dev/null; then apid=""; aud_key="__force_audio_rebuild__"; fi
  # --- video: relaunch the pipeline only on layout/source/tile-assignment change ---
  case "$layout" in single) key="single:$active";; multi) key="multi:$slots";; wall) key="wall:$slots";; *) key="$layout";; esac
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
  if [ "$layout" = "single" ]; then akey=""; else akey="$active"; fi
  adelay="$(cat "$ADELAY_FILE" 2>/dev/null)"; [[ "$adelay" =~ ^[0-9]+$ ]] || adelay=0
  if [ "$akey" != "$aud_key" ] || [ "$adelay" != "$last_adelay" ]; then
    kill_audio
    if [ "$adelay" -gt 0 ]; then ASINK="queue min-threshold-time=$((adelay*1000000)) max-size-time=$(( (adelay+3000)*1000000 )) max-size-bytes=0 max-size-buffers=0 ! pulsesink sync=false buffer-time=200000"; else ASINK="pulsesink sync=false buffer-time=200000"; fi
    if [ -n "$akey" ]; then
      acmd="$(audio_cmd "$akey")"
      [ -n "$acmd" ] && { setsid bash -c "$acmd" >/tmp/output-audio.log 2>&1 & apid=$!; }
    fi
    aud_key="$akey"; last_adelay="$adelay"
    echo "$(date +%T) audio -> ${akey:-embedded} (delay ${adelay}ms)"
  fi
  sleep 1
done
