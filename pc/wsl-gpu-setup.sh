#!/usr/bin/env bash
# Make WSL use the D3D12 GPU (NVIDIA) for OpenGL instead of CPU llvmpipe, system-wide
# for all login shells. Mesa under WSLg does not auto-select the d3d12 Gallium driver,
# so GStreamer GL (glimagesink, gl* elements) falls back to software without this.
#
# /etc/profile.d is the reliable hook: WSL `bash` sessions skip PAM, so /etc/environment
# is ignored. Run as root. Idempotent -- safe to re-run on every restore.
set -e
TARGET=/etc/profile.d/gpu-d3d12.sh
echo 'export GALLIUM_DRIVER=d3d12' > "$TARGET"
chmod 0644 "$TARGET"
echo "GPU: wrote $TARGET (GALLIUM_DRIVER=d3d12)"
