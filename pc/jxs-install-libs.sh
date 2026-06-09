#!/usr/bin/env bash
# One-time: make the locally-built SVT-JPEG-XS shared library usable by ANY user.
# The build left it under /root (mode 700), so the default user can't reach it
# and ffmpeg shows no jpegxs codec. Copy into /usr/local/lib and refresh the
# loader cache. Run once:  sudo bash pc/jxs-install-libs.sh
set -euo pipefail
SRC=/root/jxs-install/lib
if [ ! -e "$SRC/libSvtJpegxs.so" ]; then
  echo "ERROR: $SRC/libSvtJpegxs.so not found (is SVT-JPEG-XS built?)" >&2
  exit 1
fi
cp -Pv "$SRC"/libSvtJpegxs.so* /usr/local/lib/
# Drop the loader-cache entry that points into /root (mode 700). The default
# user can't traverse /root, and the loader hits that path first and fails
# ("cannot open shared object file") instead of using the /usr/local/lib copy.
# /usr/local/lib is already a default search path, so removing this is enough.
rm -f /etc/ld.so.conf.d/jxs.conf
ldconfig
echo "Registered:"
ldconfig -p | grep -i jpegxs

# High-bitrate JPEG-XS over UDP (1080p59.94 is ~380 Mbit/s) overruns the default
# UDP socket buffers and drops packets, corrupting decoded frames. Raise the
# kernel ceilings so the receiver can request a large SO_RCVBUF. Persist across
# reboots (systemd applies /etc/sysctl.d on boot) and apply now.
cat > /etc/sysctl.d/99-jxs-udp.conf <<'EOF'
net.core.rmem_max=67108864
net.core.wmem_max=67108864
EOF
sysctl -p /etc/sysctl.d/99-jxs-udp.conf
echo "UDP buffer ceilings raised."
