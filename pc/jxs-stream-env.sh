#!/usr/bin/env bash
# Shared config for the PC-native JPEG-XS streaming demo. Sourced by
# jxs-stream-send.sh and jxs-stream-view.sh so the encode geometry and the
# decode/parse caps can never drift apart. Network/transport + GPU come from atoll.conf.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/atoll.conf"   # JXS_ADDR/JXS_PORT/JXS_LOCALADDR/JXS_TTL, GALLIUM_DRIVER

# --- video geometry: encode caps and rawvideoparse caps MUST match these ---
JXS_W=1920
JXS_H=1080
JXS_FPS=60000/1001      # 59.94 fps (US cadence)
JXS_PIXFMT=yuv422p      # ffmpeg pixel format; JPEG-XS is 4:2:2
JXS_GSTFMT=Y42B         # GStreamer format token for planar yuv422p 8-bit
JXS_BPP=3               # JPEG-XS bits/pixel (quality vs bandwidth)
# JXS_ADDR / JXS_PORT / JXS_LOCALADDR / JXS_TTL come from atoll.conf
