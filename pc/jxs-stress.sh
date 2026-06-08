#!/usr/bin/env bash
# Stress comparison: push both codecs hard and zoom into the noise block so the
# difference is unmistakable. Aggressive JPEG XS (bpp1) vs low-quality JPEG (q15).
# Reuses the frame captured by jxs-demo.sh (/tmp/orig.yuv).
JXS=/root/SVT-JPEG-XS/Bin/Release
export LD_LIBRARY_PATH=$JXS
OUT=/mnt/c/Users/dgper/pi-nmos-st2110/jxs-demo
mkdir -p "$OUT"

# aggressive JPEG XS bpp1 (~16:1) then decode
$JXS/SvtJpegxsEncApp -i /tmp/orig.yuv -w 320 -h 240 --colour-format yuv422 --input-depth 8 --bpp 1 -n 1 -b /tmp/f1.jxs >/dev/null 2>&1
$JXS/SvtJpegxsDecApp -i /tmp/f1.jxs -o /tmp/dec1.yuv -n 1 >/dev/null 2>&1
# low-quality JPEG
gst-launch-1.0 -q filesrc location=/tmp/orig.yuv ! rawvideoparse format=y42b width=320 height=240 \
  ! videoconvert ! jpegenc quality=15 ! filesink location=/tmp/lowq.jpg >/dev/null 2>&1
echo "JPEG XS bpp1: $(stat -c%s /tmp/f1.jxs) bytes   |   JPEG q15: $(stat -c%s /tmp/lowq.jpg) bytes"

# zoom the bottom-right noise block (80x80) up to 480x480 with nearest-neighbour
zoom_raw() { gst-launch-1.0 -q filesrc location="$1" ! rawvideoparse format=y42b width=320 height=240 \
  ! videocrop left=240 top=160 ! videoconvert ! videoscale method=0 ! video/x-raw,width=480,height=480 \
  ! pngenc ! filesink location="$2" >/dev/null 2>&1; }
zoom_raw /tmp/orig.yuv "$OUT/zoom_1_uncompressed.png"
zoom_raw /tmp/dec1.yuv "$OUT/zoom_2_jpegxs_bpp1.png"
gst-launch-1.0 -q filesrc location=/tmp/lowq.jpg ! jpegdec ! videocrop left=240 top=160 \
  ! videoconvert ! videoscale method=0 ! video/x-raw,width=480,height=480 \
  ! pngenc ! filesink location="$OUT/zoom_3_jpeg_q15.png" >/dev/null 2>&1
echo "zoomed noise-block crops:"
ls -l "$OUT"/zoom_*.png
