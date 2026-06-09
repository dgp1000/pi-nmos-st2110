#!/usr/bin/env bash
# Shared config for the PC-native JPEG-XS streaming demo. Sourced by
# jxs-stream-send.sh and jxs-stream-view.sh so the encode geometry and the
# decode/parse caps can never drift apart.

# Use the D3D12 GPU (not CPU llvmpipe) for GStreamer GL display. Set explicitly
# so the viewer works even from a non-login shell.
export GALLIUM_DRIVER=d3d12

# --- video geometry: encode caps and rawvideoparse caps MUST match these ---
JXS_W=1920
JXS_H=1080
JXS_FPS=60000/1001      # 59.94 fps (US cadence)
JXS_PIXFMT=yuv422p      # ffmpeg pixel format; JPEG-XS is 4:2:2
JXS_GSTFMT=Y42B         # GStreamer format token for planar yuv422p 8-bit
JXS_BPP=3               # JPEG-XS bits/pixel (quality vs bandwidth)

# --- transport: MPEG-TS over UDP ---
JXS_ADDR=127.0.0.1      # loopback; use 239.10.10.22 for island multicast
JXS_PORT=5008
