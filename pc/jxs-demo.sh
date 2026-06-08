#!/usr/bin/env bash
# JPEG XS (ST 2110-22) demo: grab a real frame from the 2110-20 stream, encode it
# with SVT-JPEG-XS at several bitrates, compare sizes/ratios against JPEG, and report
# the decoder usage for the next (visual) step.
JXS=/root/SVT-JPEG-XS/Bin/Release
export LD_LIBRARY_PATH=$JXS
CAPS="application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)RAW,sampling=(string)YCbCr-4:2:2,depth=(string)8,width=(string)320,height=(string)240,payload=(int)96"

echo "=== capture a raw frame (planar 4:2:2, 320x240) ==="
rm -f /tmp/frame_*.yuv /tmp/orig.yuv
timeout 4 gst-launch-1.0 -q udpsrc address=239.10.10.20 port=5005 multicast-iface=eth1 \
  auto-multicast=true caps="$CAPS" \
  ! rtpjitterbuffer latency=100 ! rtpvrawdepay ! videoconvert \
  ! video/x-raw,format=Y42B,width=320,height=240 ! multifilesink location=/tmp/frame_%03d.yuv
FR=$(ls /tmp/frame_*.yuv 2>/dev/null | tail -2 | head -1)
[ -n "$FR" ] && cp "$FR" /tmp/orig.yuv
RAW=$(stat -c%s /tmp/orig.yuv 2>/dev/null)
echo "uncompressed frame: ${RAW:-0} bytes"
if [ -z "$RAW" ] || [ "$RAW" = "0" ]; then echo "capture failed (no video?)"; exit 1; fi

echo
echo "=== JPEG XS encodes (vs ${RAW} bytes uncompressed) ==="
for B in 2 4 6; do
  $JXS/SvtJpegxsEncApp -i /tmp/orig.yuv -w 320 -h 240 --colour-format yuv422 \
    --input-depth 8 --bpp $B -n 1 -b /tmp/f_$B.jxs >/dev/null 2>&1
  SZ=$(stat -c%s /tmp/f_$B.jxs 2>/dev/null)
  echo "  JPEG XS bpp $B -> ${SZ:-ERR} bytes  ($(awk "BEGIN{printf \"%.1f\", $RAW/${SZ:-1}}"):1)"
done

echo
echo "=== JPEG q85 (same frame, for comparison) ==="
gst-launch-1.0 -q filesrc location=/tmp/orig.yuv \
  ! rawvideoparse format=y42b width=320 height=240 ! videoconvert ! jpegenc quality=85 \
  ! filesink location=/tmp/f.jpg >/dev/null 2>&1
JP=$(stat -c%s /tmp/f.jpg 2>/dev/null)
echo "  JPEG q85 -> ${JP:-ERR} bytes  ($(awk "BEGIN{printf \"%.1f\", $RAW/${JP:-1}}"):1)"

echo
echo "=== decode JPEG XS (bpp4) and build visual comparison PNGs ==="
$JXS/SvtJpegxsDecApp -i /tmp/f_4.jxs -o /tmp/dec4.yuv -n 1 >/dev/null 2>&1
OUT=/mnt/c/Users/dgper/pi-nmos-st2110/jxs-demo
mkdir -p "$OUT"
topng() { gst-launch-1.0 -q filesrc location="$1" ! rawvideoparse format=y42b width=320 height=240 \
  ! videoconvert ! videoscale method=0 ! video/x-raw,width=640,height=480 ! pngenc \
  ! filesink location="$2" >/dev/null 2>&1; }
topng /tmp/orig.yuv "$OUT/1_uncompressed.png"
topng /tmp/dec4.yuv "$OUT/2_jpegxs_bpp4.png"
gst-launch-1.0 -q filesrc location=/tmp/f.jpg ! jpegdec ! videoconvert ! videoscale method=0 \
  ! video/x-raw,width=640,height=480 ! pngenc ! filesink location="$OUT/3_jpeg_q85.png" >/dev/null 2>&1
echo "comparison PNGs (open these in the jxs-demo folder):"
ls -l "$OUT"
