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
