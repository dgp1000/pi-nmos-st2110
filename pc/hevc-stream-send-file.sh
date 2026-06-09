#!/usr/bin/env bash
# HEVC file sender (A/V): GPU-decode a file (NVDEC), re-encode video to H.265 (NVENC),
# and mux it with the file's MP3 audio into MPEG-TS -> island multicast (tight A/V sync).
# Usage:  bash pc/hevc-stream-send-file.sh <video-file>
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/hevc-stream-env.sh"

SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "usage: $(basename "$0") <video-file>   (file not found: '$SRC')" >&2
  exit 1
fi

echo "HEVC file sender (A/V): $SRC"
echo "  -> mpegts udp://$HEVC_ADDR:$HEVC_PORT via $HEVC_IFACE  (NVDEC decode + NVENC HEVC + MP3 audio)"
echo "  Ctrl+C to stop."

# qtdemux splits the MP4: video_0 -> GPU decode (NVDEC) -> GPU re-encode HEVC (NVENC);
# audio_0 (MP3) is passed through untouched. mpegtsmux gives a shared A/V timeline;
# udpsink sync=true paces the non-live file at real time.
while true; do   # loop so the stream doesn't stop at end-of-file
  gst-launch-1.0 -q \
  filesrc location="$SRC" ! qtdemux name=d \
  d.video_0 ! h264parse ! nvh264dec ! queue \
    ! nvh265enc rc-mode=cbr bitrate="${HEVC_BITRATE}" preset=p4 tune=low-latency gop-size="${HEVC_GOP}" aud=true \
    ! h265parse config-interval=-1 ! queue ! mux. \
  d.audio_0 ! queue ! mpegaudioparse ! mux. \
  mpegtsmux name=mux alignment=7 ! queue \
    ! udpsink host="${HEVC_ADDR}" port="${HEVC_PORT}" multicast-iface="${HEVC_IFACE}" auto-multicast=true ttl="${HEVC_TTL}" buffer-size=8388608 sync=true
  echo "(reached end of file -- looping)"; sleep 0.5
done
