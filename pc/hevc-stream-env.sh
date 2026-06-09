#!/usr/bin/env bash
# Shared config for the 4K HEVC GPU pipeline (NVENC encode -> RTP multicast -> NVDEC decode).
# Sourced by hevc-stream-send.sh and hevc-stream-view.sh so send/receive stay in lock-step.

# GPU for glimagesink (D3D12, not CPU llvmpipe)
export GALLIUM_DRIVER=d3d12

# --- video geometry ---
HEVC_W=3840
HEVC_H=2160
HEVC_FPS=60000/1001        # 59.94
HEVC_BITRATE=50000         # kbit/s, CBR
HEVC_GOP=30                # IDR ~every 0.5s @ 59.94 (fast multicast late-join)
HEVC_DISPLAY_W=1920        # GPU-downscale for the display window; NVDEC still decodes full 4K
HEVC_DISPLAY_H=1080

# --- transport: RTP/H.265 multicast on the island ---
HEVC_ADDR=239.10.10.65     # mnemonic: H.265
HEVC_PORT=5010
HEVC_LOCALADDR=10.10.10.2  # island IP (Windows Ethernet adapter, mirrored into WSL)
# Detect the island NIC by its IP — WSL renames it (eth0/eth1/...) across reboots,
# so hardcoding the name breaks the multicast. Fall back to eth0 if not found.
HEVC_IFACE="$(ip -o -4 addr show 2>/dev/null | awk -v a="${HEVC_LOCALADDR}" '$4 ~ "^"a"/" {print $2; exit}')"
HEVC_IFACE="${HEVC_IFACE:-eth0}"
HEVC_TTL=1
HEVC_CAPS="application/x-rtp,media=video,encoding-name=H265,clock-rate=90000,payload=96"
