#!/usr/bin/env bash
# Shared config for the 4K HEVC GPU pipeline (NVENC encode -> RTP multicast -> NVDEC decode).
# Sourced by hevc-stream-send.sh and hevc-stream-view.sh. Network/transport + GPU from atoll.conf.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/atoll.conf"   # HEVC_ADDR/HEVC_PORT/HEVC_IFACE/HEVC_LOCALADDR/HEVC_TTL, GALLIUM_DRIVER

# --- video geometry ---
HEVC_W=3840
HEVC_H=2160
HEVC_FPS=60000/1001        # 59.94
HEVC_BITRATE=50000         # kbit/s, CBR
HEVC_GOP=30                # IDR ~every 0.5s @ 59.94 (fast multicast late-join)
HEVC_DISPLAY_W=1920        # display-window size (downscaled from the full-4K decode); smooth at 1080p
HEVC_DISPLAY_H=1080
HEVC_CAPS="application/x-rtp,media=video,encoding-name=H265,clock-rate=90000,payload=96"
# HEVC_ADDR / HEVC_PORT / HEVC_IFACE / HEVC_LOCALADDR / HEVC_TTL come from atoll.conf
